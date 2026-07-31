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


class HermesAPIError(RuntimeError):
    """Raised when the Hermes API returns an invalid or unsuccessful response."""


class HermesClient:
    """Small adapter around Hermes' native runs API."""

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
        """Submit a Hermes run and optionally wait for its terminal result."""

        payload: dict[str, Any] = {
            "input": prompt,
            "model": self._settings.hermes_model,
        }
        if session_id:
            payload["session_id"] = session_id
        instructions = self._instructions(agent, subagents, orchestration)
        if instructions:
            payload["instructions"] = instructions

        async with self._client() as client:
            response = await client.post("/v1/runs", json=payload)
            data = self._decode(response, expected={200, 201, 202})

        execution_id = str(data.get("run_id") or data.get("id") or "")
        if not execution_id:
            raise HermesAPIError("Hermes did not return run_id or id")

        initial = self._normalize(
            data,
            execution_id=execution_id,
            fallback_session_id=session_id,
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
            fallback_session_id=session_id,
            agent=agent,
            subagents=subagents,
        )

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
            response = await client.get(f"/v1/runs/{execution_id}")
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
        while latest.status not in TERMINAL_STATUSES and loop.time() < deadline:
            await asyncio.sleep(self._settings.hermes_run_poll_interval_seconds)
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
            response = await client.post(f"/v1/runs/{execution_id}/stop")
            data = self._decode(response, expected={200, 202})
        return self._normalize(data, execution_id=execution_id)

    async def health(self, *, detailed: bool = False) -> dict[str, Any]:
        """Return Hermes liveness or authenticated readiness information."""

        path = "/health/detailed" if detailed else "/health"
        async with self._client() as client:
            response = await client.get(path)
            return self._decode(response, expected={200})

    @staticmethod
    def _decode(response: httpx.Response, *, expected: set[int]) -> dict[str, Any]:
        if response.status_code not in expected:
            body = response.text[:2000]
            raise HermesAPIError(
                f"Hermes API returned HTTP {response.status_code}: {body or '<empty>'}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise HermesAPIError("Hermes API returned non-JSON content") from exc
        if not isinstance(data, dict):
            raise HermesAPIError("Hermes API returned a non-object JSON response")
        return data

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
        return HermesPromptResult(
            session_id=data.get("session_id") or fallback_session_id,
            execution_id=execution_id,
            status=cls._normalize_status(data.get("status")),
            output=data.get("output") or data.get("response"),
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
