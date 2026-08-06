"""HTTP client for the hermes-agent API server."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .models import (
    TERMINAL_STATUSES,
    HermesPromptResult,
    RunStatus,
)
from .observability import (
    record_polling_iteration,
    record_sse_connection,
    record_sse_fallback,
    record_upstream,
)
from .protocol import OrchestrationMode

logger = logging.getLogger(__name__)

TransportFactory = Callable[[], httpx.AsyncBaseTransport]
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]
_HISTORY_ROLES = frozenset({"system", "user", "assistant"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_PROMPT_CHARS = 200_000
_MAX_AGENT_HINT_CHARS = 128
_MAX_SUBAGENTS = 16
_SESSION_TITLE_MAX_CHARS = 80
_SESSION_CREATE_ATTEMPTS = 3
_TERMINAL_EVENT_TYPES = frozenset({"run.completed", "run.failed", "run.cancelled"})


@dataclass(frozen=True)
class _EventStreamEnd:
    error: str | None = None


class HermesAPIError(RuntimeError):
    """Raised when the Hermes API is unavailable or returns an invalid response."""


class HermesClient:
    """Small adapter around Hermes' native session and runs APIs."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._settings = settings
        self._transport_factory = transport_factory

    def _client(self, *, event_stream: bool = False) -> httpx.AsyncClient:
        transport = self._transport_factory() if self._transport_factory else None
        if event_stream:
            timeout = httpx.Timeout(
                connect=self._settings.hermes_event_stream_connect_timeout_seconds,
                read=None,
                write=self._settings.hermes_request_timeout_seconds,
                pool=self._settings.hermes_request_timeout_seconds,
            )
        else:
            timeout = httpx.Timeout(self._settings.hermes_request_timeout_seconds)
        return httpx.AsyncClient(
            base_url=self._settings.hermes_api_base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self._settings.hermes_api_key.get_secret_value()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    @staticmethod
    def _instructions(
        agent: str | None,
        subagents: list[str] | None,
        orchestration: OrchestrationMode,
    ) -> str | None:
        if agent is not None and len(agent) > _MAX_AGENT_HINT_CHARS:
            raise HermesAPIError("Agent hint is too long")
        if subagents is not None:
            if len(subagents) > _MAX_SUBAGENTS:
                raise HermesAPIError("Too many subagent hints")
            if any(len(name) > _MAX_AGENT_HINT_CHARS for name in subagents):
                raise HermesAPIError("A subagent hint is too long")

        parts: list[str] = []
        if agent:
            parts.append(f"Use the Hermes agent/profile '{agent}' as the primary agent.")
        if subagents:
            names = ", ".join(f"'{name}'" for name in subagents)
            parts.append(f"Use these subagents when appropriate: {names}.")
        if orchestration == OrchestrationMode.EXPLICIT:
            parts.append(
                "Follow the requested agent assignment explicitly and report which "
                "agents were used."
            )
        return " ".join(parts) or None

    async def submit_prompt(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        agent: str | None = None,
        subagents: list[str] | None = None,
        orchestration: OrchestrationMode = OrchestrationMode.AUTO,
        wait_seconds: float | None = None,
        progress_callback: ProgressCallback | None = None,
        stop_on_cancel: bool = False,
    ) -> HermesPromptResult:
        """Submit a Hermes run and optionally keep the request connected."""

        max_wait = self._bounded_wait(wait_seconds)

        result = await self.create_run(
            prompt=prompt,
            session_id=session_id,
            agent=agent,
            subagents=subagents,
            orchestration=orchestration,
        )
        await self._notify_progress(
            progress_callback,
            {
                "event": "bridge.run.accepted",
                "run_id": result.execution_id,
                "session_id": result.session_id,
                "status": result.status.value,
            },
        )
        if result.status in TERMINAL_STATUSES or max_wait == 0:
            return result

        try:
            return await self.wait_for_run(
                result.execution_id,
                max_wait_seconds=max_wait,
                fallback_session_id=result.session_id,
                agent=agent,
                subagents=subagents,
                progress_callback=progress_callback,
            )
        except asyncio.CancelledError:
            if stop_on_cancel:
                with suppress(HermesAPIError):
                    await asyncio.shield(self.stop_run(result.execution_id))
            raise

    async def create_run(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        agent: str | None = None,
        subagents: list[str] | None = None,
        orchestration: OrchestrationMode = OrchestrationMode.AUTO,
    ) -> HermesPromptResult:
        """Validate the prompt, prepare a session, submit a run, and return immediately."""

        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise HermesAPIError("Prompt must not be empty")
        if len(normalized_prompt) > _MAX_PROMPT_CHARS:
            raise HermesAPIError("Prompt exceeds the bridge size limit")

        instructions = self._instructions(agent, subagents, orchestration)
        async with self._client() as http_client:
            active_session_id, conversation_history = await self._prepare_session(
                http_client,
                prompt=normalized_prompt,
                session_id=session_id,
            )
            payload: dict[str, Any] = {
                "input": normalized_prompt,
                "model": self._settings.hermes_model,
                "session_id": active_session_id,
            }
            if conversation_history:
                payload["conversation_history"] = conversation_history
            if instructions:
                payload["instructions"] = instructions

            response = await self._send(http_client, "POST", "/v1/runs", json=payload)
            data = self._decode(response, expected={200, 201, 202})

        execution_id = str(data.get("run_id") or data.get("id") or "")
        self._validate_identifier(execution_id, label="execution_id")
        return self._normalize(
            data,
            execution_id=execution_id,
            fallback_session_id=active_session_id,
            agent=agent,
            subagents=subagents,
        )

    def _bounded_wait(self, wait_seconds: float | None) -> float:
        configured_max = self._settings.hermes_run_max_wait_seconds
        default_wait = getattr(
            self._settings,
            "hermes_run_default_wait_seconds",
            configured_max,
        )
        bounded_default = min(default_wait, configured_max)
        if wait_seconds is None:
            return bounded_default
        if not math.isfinite(wait_seconds):
            raise HermesAPIError("wait_seconds must be a finite number")
        return min(max(0.0, wait_seconds), configured_max)

    async def _prepare_session(
        self,
        client: httpx.AsyncClient,
        *,
        prompt: str,
        session_id: str | None,
    ) -> tuple[str, list[dict[str, str]]]:
        if session_id is None:
            return await self._create_session(client, prompt=prompt), []
        self._validate_identifier(session_id, label="session_id")
        return await self._load_session_history(client, session_id=session_id)

    async def _create_session(self, client: httpx.AsyncClient, *, prompt: str) -> str:
        last_response: httpx.Response | None = None
        for _ in range(_SESSION_CREATE_ATTEMPTS):
            payload = {
                "title": self._session_title(prompt),
                "model": self._settings.hermes_model,
            }
            response = await self._send(client, "POST", "/api/sessions", json=payload)
            last_response = response
            if response.status_code == 201:
                data = self._decode(response, expected={201})
                session = data.get("session")
                session_id = session.get("id") if isinstance(session, dict) else None
                if not isinstance(session_id, str):
                    raise HermesAPIError("Hermes did not return a native session id")
                self._validate_identifier(session_id, label="session_id")
                return session_id
            if not self._is_duplicate_session_title(response):
                self._decode(response, expected={201})

        if last_response is None:
            raise HermesAPIError("Hermes session creation did not run")
        raise HermesAPIError(
            "Hermes rejected multiple unique session titles; session was not created"
        )

    async def _load_session_history(
        self,
        client: httpx.AsyncClient,
        *,
        session_id: str,
    ) -> tuple[str, list[dict[str, str]]]:
        response = await self._send(
            client,
            "GET",
            f"/api/sessions/{session_id}/messages",
        )
        if response.status_code == 404:
            raise HermesAPIError(f"Hermes session not found: {session_id}")
        data = self._decode(response, expected={200})

        resolved_session_id = data.get("session_id") or session_id
        if not isinstance(resolved_session_id, str):
            resolved_session_id = session_id
        self._validate_identifier(resolved_session_id, label="session_id")

        raw_messages = data.get("data")
        if not isinstance(raw_messages, list):
            raise HermesAPIError("Hermes session history returned an invalid data field")

        history: list[dict[str, str]] = []
        for message in raw_messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role in _HISTORY_ROLES and isinstance(content, str):
                history.append({"role": role, "content": content})
        return resolved_session_id, history

    @staticmethod
    def _session_title(prompt: str) -> str:
        suffix = f" [mcp-{uuid.uuid4().hex[:12]}]"
        first_line = prompt.splitlines()[0].strip() or "MCP delegated task"
        available = _SESSION_TITLE_MAX_CHARS - len(suffix)
        return f"{first_line[:available]}{suffix}"

    @staticmethod
    def _is_duplicate_session_title(response: httpx.Response) -> bool:
        if response.status_code != 400:
            return False
        return "title already in use" in HermesClient._error_detail(response).lower()

    @staticmethod
    def _validate_identifier(value: str, *, label: str) -> None:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise HermesAPIError(f"Invalid Hermes {label}")

    async def get_run(
        self,
        execution_id: str,
        *,
        fallback_session_id: str | None = None,
        agent: str | None = None,
        subagents: list[str] | None = None,
    ) -> HermesPromptResult:
        """Retrieve and normalize one run."""

        self._validate_identifier(execution_id, label="execution_id")
        async with self._client() as client:
            response = await self._send(client, "GET", f"/v1/runs/{execution_id}")
            data = self._decode(response, expected={200})
        return self._normalize(
            data,
            execution_id=execution_id,
            fallback_session_id=fallback_session_id,
            agent=agent,
            subagents=subagents,
        )

    async def wait_for_run(
        self,
        execution_id: str,
        *,
        max_wait_seconds: float,
        fallback_session_id: str | None = None,
        agent: str | None = None,
        subagents: list[str] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> HermesPromptResult:
        """Wait for a run using Hermes SSE events with polling fallback."""

        self._validate_identifier(execution_id, label="execution_id")
        if max_wait_seconds <= 0:
            return await self.get_run(
                execution_id,
                fallback_session_id=fallback_session_id,
                agent=agent,
                subagents=subagents,
            )
        if progress_callback is None:
            return await self._wait_for_run_polling(
                execution_id,
                max_wait_seconds=max_wait_seconds,
                fallback_session_id=fallback_session_id,
                agent=agent,
                subagents=subagents,
            )
        return await self._wait_for_run_connected(
            execution_id,
            max_wait_seconds=max_wait_seconds,
            fallback_session_id=fallback_session_id,
            agent=agent,
            subagents=subagents,
            progress_callback=progress_callback,
        )

    async def _wait_for_run_connected(
        self,
        execution_id: str,
        *,
        max_wait_seconds: float,
        fallback_session_id: str | None,
        agent: str | None,
        subagents: list[str] | None,
        progress_callback: ProgressCallback,
    ) -> HermesPromptResult:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + max_wait_seconds
        queue: asyncio.Queue[dict[str, Any] | _EventStreamEnd] = asyncio.Queue()
        reader_task = asyncio.create_task(self._read_run_events(execution_id, queue))
        terminal_event_seen = False
        stream_error: str | None = None
        next_heartbeat_at = started_at + self._settings.hermes_progress_interval_seconds

        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                timeout = min(self._settings.hermes_progress_interval_seconds, remaining)
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    latest = await self.get_run(
                        execution_id,
                        fallback_session_id=fallback_session_id,
                        agent=agent,
                        subagents=subagents,
                    )
                    await self._notify_progress(
                        progress_callback,
                        {
                            "event": "bridge.heartbeat",
                            "run_id": execution_id,
                            "status": latest.status.value,
                            "elapsed_seconds": round(loop.time() - started_at, 1),
                        },
                    )
                    next_heartbeat_at = (
                        loop.time() + self._settings.hermes_progress_interval_seconds
                    )
                    if latest.status in TERMINAL_STATUSES:
                        return latest
                    continue

                if isinstance(item, _EventStreamEnd):
                    stream_error = item.error
                    break

                await self._notify_progress(progress_callback, item)
                event_type = str(item.get("event") or "")
                if event_type in _TERMINAL_EVENT_TYPES:
                    terminal_event_seen = True
                    break
                if loop.time() >= next_heartbeat_at:
                    await self._notify_progress(
                        progress_callback,
                        {
                            "event": "bridge.heartbeat",
                            "run_id": execution_id,
                            "status": "running",
                            "elapsed_seconds": round(loop.time() - started_at, 1),
                        },
                    )
                    next_heartbeat_at = (
                        loop.time() + self._settings.hermes_progress_interval_seconds
                    )
        finally:
            if not reader_task.done():
                reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await reader_task

        if terminal_event_seen:
            return await self.get_run(
                execution_id,
                fallback_session_id=fallback_session_id,
                agent=agent,
                subagents=subagents,
            )

        remaining = max(0.0, deadline - loop.time())
        record_sse_fallback(stream_error or "stream_ended")
        await self._notify_progress(
            progress_callback,
            {
                "event": "bridge.event_stream_fallback",
                "run_id": execution_id,
                "error": stream_error,
                "remaining_seconds": round(remaining, 1),
            },
        )
        return await self._wait_for_run_polling(
            execution_id,
            max_wait_seconds=remaining,
            fallback_session_id=fallback_session_id,
            agent=agent,
            subagents=subagents,
            progress_callback=progress_callback,
            started_at=started_at,
        )

    async def _wait_for_run_polling(
        self,
        execution_id: str,
        *,
        max_wait_seconds: float,
        fallback_session_id: str | None = None,
        agent: str | None = None,
        subagents: list[str] | None = None,
        progress_callback: ProgressCallback | None = None,
        started_at: float | None = None,
    ) -> HermesPromptResult:
        loop = asyncio.get_running_loop()
        started_at = loop.time() if started_at is None else started_at
        deadline = loop.time() + max_wait_seconds
        next_progress_at = loop.time() + self._settings.hermes_progress_interval_seconds
        latest = await self.get_run(
            execution_id,
            fallback_session_id=fallback_session_id,
            agent=agent,
            subagents=subagents,
        )
        while latest.status not in TERMINAL_STATUSES:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._notify_progress(
                    progress_callback,
                    {
                        "event": "bridge.wait_expired",
                        "run_id": execution_id,
                        "status": latest.status.value,
                        "elapsed_seconds": round(loop.time() - started_at, 1),
                    },
                )
                break
            await asyncio.sleep(
                min(self._settings.hermes_run_poll_interval_seconds, remaining)
            )
            record_polling_iteration()
            latest = await self.get_run(
                execution_id,
                fallback_session_id=fallback_session_id,
                agent=agent,
                subagents=subagents,
            )
            if loop.time() >= next_progress_at and latest.status not in TERMINAL_STATUSES:
                await self._notify_progress(
                    progress_callback,
                    {
                        "event": "bridge.heartbeat",
                        "run_id": execution_id,
                        "status": latest.status.value,
                        "elapsed_seconds": round(loop.time() - started_at, 1),
                    },
                )
                next_progress_at = (
                    loop.time() + self._settings.hermes_progress_interval_seconds
                )
        return latest

    async def _read_run_events(
        self,
        execution_id: str,
        queue: asyncio.Queue[dict[str, Any] | _EventStreamEnd],
    ) -> None:
        try:
            async with (
                self._client(event_stream=True) as client,
                client.stream(
                    "GET",
                    f"/v1/runs/{execution_id}/events",
                    headers={"Accept": "text/event-stream"},
                ) as response,
            ):
                if response.status_code != 200:
                    await response.aread()
                    record_sse_connection("rejected")
                    raise HermesAPIError(
                        "Hermes event stream returned "
                        f"HTTP {response.status_code}: {self._error_detail(response)}"
                    )
                record_sse_connection("open")
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if not line:
                        await self._queue_sse_event(data_lines, queue)
                        data_lines.clear()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                await self._queue_sse_event(data_lines, queue)
            await queue.put(_EventStreamEnd())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(_EventStreamEnd(error=str(exc)))

    @staticmethod
    async def _queue_sse_event(
        data_lines: list[str],
        queue: asyncio.Queue[dict[str, Any] | _EventStreamEnd],
    ) -> None:
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(event, dict):
            await queue.put(event)

    @staticmethod
    async def _notify_progress(
        callback: ProgressCallback | None,
        event: dict[str, Any],
    ) -> None:
        if callback is None:
            return
        try:
            await callback(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "progress callback failed; event=%s exception=%s",
                event.get("event"),
                exc.__class__.__name__,
            )

    async def stop_run(self, execution_id: str) -> HermesPromptResult:
        """Ask Hermes to stop a run at its next safe interruption point."""

        self._validate_identifier(execution_id, label="execution_id")
        async with self._client() as client:
            response = await self._send(
                client,
                "POST",
                f"/v1/runs/{execution_id}/stop",
            )
            data = self._decode(response, expected={200, 202})
        return self._normalize(data, execution_id=execution_id)

    async def health(self, *, detailed: bool = False) -> dict[str, Any]:
        """Return Hermes liveness or authenticated readiness information."""

        path = "/health/detailed" if detailed else "/health"
        async with self._client() as client:
            response = await self._send(client, "GET", path)
            return self._decode(response, expected={200})

    @staticmethod
    async def _send(
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        started = time.perf_counter()
        status_code: int | None = None
        outcome = "success"
        try:
            response = await client.request(method, path, **kwargs)
            status_code = response.status_code
            if status_code >= 500:
                outcome = "upstream_error"
            elif status_code >= 400:
                outcome = "client_error"
            return response
        except httpx.TimeoutException as exc:
            outcome = "timeout"
            raise HermesAPIError("Hermes API request timed out") from exc
        except httpx.RequestError as exc:
            outcome = "unreachable"
            raise HermesAPIError("Unable to reach the Hermes API server") from exc
        finally:
            record_upstream(
                path=path,
                status_code=status_code,
                duration_seconds=time.perf_counter() - started,
                outcome=outcome,
            )

    @staticmethod
    def _decode(response: httpx.Response, *, expected: set[int]) -> dict[str, Any]:
        if response.status_code not in expected:
            detail = HermesClient._error_detail(response)
            raise HermesAPIError(
                f"Hermes API returned HTTP {response.status_code}: {detail}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise HermesAPIError("Hermes API returned non-JSON content") from exc
        if not isinstance(data, dict):
            raise HermesAPIError("Hermes API returned a non-object JSON response")
        return data

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.reason_phrase or "non-JSON error response"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()[:500]
            if isinstance(error, str) and error.strip():
                return error.strip()[:500]
        return "unexpected error response"

    @staticmethod
    def _normalize_status(raw: Any) -> RunStatus:
        value = str(raw or "unknown").lower()
        aliases = {
            "created": RunStatus.QUEUED,
            "pending": RunStatus.QUEUED,
            "in_progress": RunStatus.RUNNING,
            "stopped": RunStatus.CANCELLED,
        }
        alias = aliases.get(value)
        if alias is not None:
            return alias
        try:
            return RunStatus(value)
        except ValueError:
            return RunStatus.UNKNOWN

    @classmethod
    def _normalize(
        cls,
        data: dict[str, Any],
        *,
        execution_id: str,
        fallback_session_id: str | None = None,
        agent: str | None = None,
        subagents: list[str] | None = None,
    ) -> HermesPromptResult:
        error = data.get("error")
        if isinstance(error, dict):
            error = error.get("message") or str(error)
        output = data.get("output") or data.get("response")
        if output is not None and not isinstance(output, str):
            output = str(output)
        return HermesPromptResult(
            session_id=data.get("session_id") or fallback_session_id,
            execution_id=execution_id,
            status=cls._normalize_status(data.get("status")),
            output=output,
            error=str(error) if error else None,
            agent=data.get("agent") or agent,
            subagents=list(data.get("subagents") or subagents or []),
            metadata={
                key: value
                for key, value in data.items()
                if key
                not in {
                    "run_id",
                    "id",
                    "session_id",
                    "status",
                    "output",
                    "response",
                    "error",
                    "agent",
                    "subagents",
                }
            },
        )
