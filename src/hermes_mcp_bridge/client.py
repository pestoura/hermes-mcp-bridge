"""HTTP client for the hermes-agent API server."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from .config import Settings
from .models import (
    TERMINAL_STATUSES,
    HermesPromptResult,
    OrchestrationMode,
    RunStatus,
)

TransportFactory = Callable[[], httpx.AsyncBaseTransport]
_HISTORY_ROLES = frozenset({"system", "user", "assistant"})


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

    def _client(self) -> httpx.AsyncClient:
        transport = self._transport_factory() if self._transport_factory else None
        return httpx.AsyncClient(
            base_url=self._settings.hermes_api_base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self._settings.hermes_api_key.get_secret_value()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(self._settings.hermes_request_timeout_seconds),
            transport=transport,
        )

    @staticmethod
    def _instructions(
        agent: str | None,
        subagents: list[str] | None,
        orchestration: OrchestrationMode,
    ) -> str | None:
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
    ) -> HermesPromptResult:
        """Submit a Hermes run and optionally wait for its terminal result.

        A missing ``session_id`` creates a native Hermes session. When a session
        is supplied, its persisted message history is loaded and forwarded to
        ``/v1/runs`` because that endpoint treats ``session_id`` as a run scope,
        not as an instruction to load previous messages automatically.
        """

        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise HermesAPIError("Prompt must not be empty")

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
            instructions = self._instructions(agent, subagents, orchestration)
            if instructions:
                payload["instructions"] = instructions

            response = await self._send(http_client, "POST", "/v1/runs", json=payload)
            data = self._decode(response, expected={200, 201, 202})

        execution_id = str(data.get("run_id") or data.get("id") or "")
        if not execution_id:
            raise HermesAPIError("Hermes did not return run_id or id")

        initial = self._normalize(
            data,
            execution_id=execution_id,
            fallback_session_id=active_session_id,
            agent=agent,
            subagents=subagents,
        )
        if initial.status in TERMINAL_STATUSES:
            return initial

        max_wait = (
            self._settings.hermes_run_max_wait_seconds
            if wait_seconds is None
            else max(0.0, wait_seconds)
        )
        if max_wait == 0:
            return initial

        return await self.wait_for_run(
            execution_id,
            max_wait_seconds=max_wait,
            fallback_session_id=active_session_id,
            agent=agent,
            subagents=subagents,
        )

    async def _prepare_session(
        self,
        client: httpx.AsyncClient,
        *,
        prompt: str,
        session_id: str | None,
    ) -> tuple[str, list[dict[str, str]]]:
        if session_id is None:
            return await self._create_session(client, prompt=prompt), []
        return await self._load_session_history(client, session_id=session_id)

    async def _create_session(self, client: httpx.AsyncClient, *, prompt: str) -> str:
        payload = {
            "title": self._session_title(prompt),
            "model": self._settings.hermes_model,
        }
        response = await self._send(client, "POST", "/api/sessions", json=payload)
        data = self._decode(response, expected={201})
        session = data.get("session")
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or not session_id.strip():
            raise HermesAPIError("Hermes did not return a native session id")
        return session_id

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
        if not isinstance(resolved_session_id, str) or not resolved_session_id.strip():
            resolved_session_id = session_id

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
        first_line = prompt.splitlines()[0].strip()
        return (first_line or "MCP delegated task")[:80]

    async def get_run(
        self,
        execution_id: str,
        *,
        fallback_session_id: str | None = None,
        agent: str | None = None,
        subagents: list[str] | None = None,
    ) -> HermesPromptResult:
        """Retrieve and normalize one run."""

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
    ) -> HermesPromptResult:
        """Poll until the run finishes or the MCP-side wait budget expires."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_seconds
        latest = await self.get_run(
            execution_id,
            fallback_session_id=fallback_session_id,
            agent=agent,
            subagents=subagents,
        )
        while latest.status not in TERMINAL_STATUSES:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(
                min(self._settings.hermes_run_poll_interval_seconds, remaining)
            )
            latest = await self.get_run(
                execution_id,
                fallback_session_id=fallback_session_id,
                agent=agent,
                subagents=subagents,
            )
        return latest

    async def stop_run(self, execution_id: str) -> HermesPromptResult:
        """Ask Hermes to stop a run at its next safe interruption point."""

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
        try:
            return await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise HermesAPIError("Hermes API request timed out") from exc
        except httpx.RequestError as exc:
            raise HermesAPIError("Unable to reach the Hermes API server") from exc

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
            return response.text.strip()[:500] or "empty response"
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
