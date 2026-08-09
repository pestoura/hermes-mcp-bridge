from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.client import HermesClient
from hermes_mcp_bridge.config import Settings


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "hermes_api_key": SecretStr("test-key"),
        "hermes_api_base_url": "http://hermes.test",
        "hermes_run_poll_interval_seconds": 0.001,
        "hermes_run_max_wait_seconds": 7200.0,
        "hermes_run_default_wait_seconds": 45.0,
        "hermes_progress_interval_seconds": 0.01,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_default_wait_is_forty_five_and_capped_at_max() -> None:
    client = HermesClient(
        settings(hermes_run_default_wait_seconds=45.0, hermes_run_max_wait_seconds=7200.0)
    )
    assert client._bounded_wait(None) == pytest.approx(45.0)
    assert client._bounded_wait(7200.0) == pytest.approx(7200.0)
    assert client._bounded_wait(9_999.0) == pytest.approx(7200.0)
    assert client._bounded_wait(0) == 0.0


def test_default_wait_is_capped_when_higher_than_max() -> None:
    client = HermesClient(
        settings(hermes_run_default_wait_seconds=10_000.0, hermes_run_max_wait_seconds=7200.0)
    )
    assert client._bounded_wait(None) == pytest.approx(7200.0)


@pytest.mark.asyncio
async def test_create_run_single_post_and_returns_immediately() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(
                201, json={"object": "hermes.session", "session": {"id": "session-new"}}
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "run-1", "status": "started"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(settings(), transport_factory=lambda: httpx.MockTransport(handler))
    result = await client.create_run(prompt="Standalone task")

    assert result.execution_id == "run-1"
    assert result.status.value == "started"
    assert len([request for request in requests if request.method == "POST"]) == 2
    assert not any(request.method == "GET" for request in requests)


@pytest.mark.asyncio
async def test_submit_prompt_wait_zero_returns_immediately() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(
                201, json={"object": "hermes.session", "session": {"id": "session-new"}}
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "run-1", "status": "started"})
        if request.method == "GET" and request.url.path == "/v1/runs/run-1":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "session_id": "session-new",
                    "status": "completed",
                    "output": "Service Z is healthy.",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(settings(), transport_factory=lambda: httpx.MockTransport(handler))
    result = await client.submit_prompt(prompt="Task", wait_seconds=0)

    assert result.execution_id == "run-1"
    assert result.status.value == "started"
    assert not any(
        request.method == "GET" and request.url.path == "/v1/runs/run-1" for request in requests
    )


@pytest.mark.asyncio
async def test_progress_callback_exception_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    sensitive_message = "token abc123 leaked"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(
                201, json={"object": "hermes.session", "session": {"id": "session-new"}}
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "run-1", "status": "started"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(settings(), transport_factory=lambda: httpx.MockTransport(handler))

    async def failing_progress(event: dict[str, object]) -> None:
        raise RuntimeError(sensitive_message)

    with caplog.at_level("WARNING"):
        result = await client.submit_prompt(
            prompt="Task", wait_seconds=0, progress_callback=failing_progress
        )

    assert result.execution_id == "run-1"
    assert sensitive_message not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "event=" in caplog.text
