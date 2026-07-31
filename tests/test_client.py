from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.client import HermesAPIError, HermesClient
from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.models import OrchestrationMode, RunStatus


def settings() -> Settings:
    return Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        hermes_run_poll_interval_seconds=0.001,
        hermes_run_max_wait_seconds=0.1,
    )


@pytest.mark.asyncio
async def test_submit_prompt_waits_for_completion() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.headers["authorization"] == "Bearer test-key"
        if request.method == "POST" and request.url.path == "/v1/runs":
            body = json.loads(request.content)
            assert body["input"] == "Validate service Z"
            assert body["session_id"] == "session-1"
            assert "infra" in body["instructions"]
            assert "security" in body["instructions"]
            return httpx.Response(202, json={"run_id": "run-1", "status": "started"})
        if request.method == "GET" and request.url.path == "/v1/runs/run-1":
            calls += 1
            if calls == 1:
                return httpx.Response(200, json={"run_id": "run-1", "status": "running"})
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "session_id": "session-1",
                    "status": "completed",
                    "output": "Service Z is healthy.",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )
    result = await client.submit_prompt(
        prompt="Validate service Z",
        session_id="session-1",
        agent="infra",
        subagents=["security"],
        orchestration=OrchestrationMode.EXPLICIT,
    )

    assert result.execution_id == "run-1"
    assert result.status == RunStatus.COMPLETED
    assert result.output == "Service Z is healthy."
    assert result.session_id == "session-1"


@pytest.mark.asyncio
async def test_submit_prompt_can_return_immediately() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/runs"
        return httpx.Response(202, json={"run_id": "run-2", "status": "started"})

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )
    result = await client.submit_prompt(prompt="Long task", wait_seconds=0)

    assert result.execution_id == "run-2"
    assert result.status == RunStatus.STARTED


@pytest.mark.asyncio
async def test_api_error_is_sanitized_to_bounded_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="denied")

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )

    with pytest.raises(HermesAPIError, match="HTTP 401: denied"):
        await client.health()
