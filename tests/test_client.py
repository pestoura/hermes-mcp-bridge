from __future__ import annotations

import json
import math
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.client import HermesAPIError, HermesClient
from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.models import RunStatus
from hermes_mcp_bridge.protocol import OrchestrationMode


def settings() -> Settings:
    return Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        hermes_run_poll_interval_seconds=0.001,
        hermes_run_max_wait_seconds=0.1,
    )


@pytest.mark.asyncio
async def test_submit_prompt_loads_session_history_and_waits_for_completion() -> None:
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        assert request.headers["authorization"] == "Bearer test-key"
        if request.method == "GET" and request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "session_id": "session-1",
                    "data": [
                        {"role": "user", "content": "Earlier request", "timestamp": 1},
                        {
                            "role": "assistant",
                            "content": "Earlier answer",
                            "timestamp": 2,
                        },
                    ],
                },
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            body = json.loads(request.content)
            assert body["input"] == "Validate service Z"
            assert body["session_id"] == "session-1"
            assert body["conversation_history"] == [
                {"role": "user", "content": "Earlier request"},
                {"role": "assistant", "content": "Earlier answer"},
            ]
            assert "infra" in body["instructions"]
            assert "security" in body["instructions"]
            return httpx.Response(202, json={"run_id": "run-1", "status": "started"})
        if request.method == "GET" and request.url.path == "/v1/runs/run-1":
            status_calls += 1
            if status_calls == 1:
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
async def test_submit_prompt_creates_native_session_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hermes_mcp_bridge.client.uuid.uuid4",
        lambda: SimpleNamespace(hex="1234567890abcdef"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sessions":
            body = json.loads(request.content)
            assert body["title"] == "Long task [mcp-1234567890ab]"
            assert body["model"] == "hermes-agent"
            return httpx.Response(
                201,
                json={"object": "hermes.session", "session": {"id": "session-new"}},
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            body = json.loads(request.content)
            assert body["session_id"] == "session-new"
            assert "conversation_history" not in body
            return httpx.Response(202, json={"run_id": "run-2", "status": "started"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )
    result = await client.submit_prompt(prompt="Long task", wait_seconds=0)

    assert result.execution_id == "run-2"
    assert result.session_id == "session-new"
    assert result.status == RunStatus.STARTED


@pytest.mark.asyncio
async def test_duplicate_session_title_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifiers = iter(("aaaaaaaaaaaa0000", "bbbbbbbbbbbb0000"))
    monkeypatch.setattr(
        "hermes_mcp_bridge.client.uuid.uuid4",
        lambda: SimpleNamespace(hex=next(identifiers)),
    )
    titles: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sessions":
            body = json.loads(request.content)
            titles.append(body["title"])
            if len(titles) == 1:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Title already in use by session api_existing"
                        }
                    },
                )
            return httpx.Response(
                201,
                json={"object": "hermes.session", "session": {"id": "session-retry"}},
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "run-retry", "status": "started"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )
    result = await client.submit_prompt(prompt="Repeated task", wait_seconds=0)

    assert titles == [
        "Repeated task [mcp-aaaaaaaaaaaa]",
        "Repeated task [mcp-bbbbbbbbbbbb]",
    ]
    assert result.session_id == "session-retry"
    assert result.execution_id == "run-retry"


def test_session_title_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_mcp_bridge.client.uuid.uuid4",
        lambda: SimpleNamespace(hex="1234567890abcdef"),
    )

    title = HermesClient._session_title("x" * 200)

    assert len(title) == 80
    assert title.endswith(" [mcp-1234567890ab]")


@pytest.mark.asyncio
async def test_compression_tip_session_id_is_used_for_new_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={"session_id": "tip-session", "data": []},
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            body = json.loads(request.content)
            assert body["session_id"] == "tip-session"
            return httpx.Response(202, json={"run_id": "run-tip", "status": "started"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )
    result = await client.submit_prompt(
        prompt="Continue",
        session_id="source-session",
        wait_seconds=0,
    )

    assert result.session_id == "tip-session"


@pytest.mark.asyncio
async def test_unknown_session_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )

    with pytest.raises(HermesAPIError, match="Hermes session not found"):
        await client.submit_prompt(prompt="Continue", session_id="missing")


@pytest.mark.asyncio
async def test_empty_prompt_is_rejected_before_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )

    with pytest.raises(HermesAPIError, match="must not be empty"):
        await client.submit_prompt(prompt="  \n ")


@pytest.mark.asyncio
async def test_invalid_session_identifier_is_rejected_before_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )

    with pytest.raises(HermesAPIError, match="Invalid Hermes session_id"):
        await client.submit_prompt(prompt="Continue", session_id="../other-session")


@pytest.mark.asyncio
async def test_invalid_execution_identifier_is_rejected_before_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )

    with pytest.raises(HermesAPIError, match="Invalid Hermes execution_id"):
        await client.get_run("../../run")
    with pytest.raises(HermesAPIError, match="Invalid Hermes execution_id"):
        await client.stop_run("run/other")


@pytest.mark.asyncio
async def test_non_finite_wait_is_rejected_before_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )

    for value in (math.inf, -math.inf, math.nan):
        with pytest.raises(HermesAPIError, match="finite number"):
            await client.submit_prompt(prompt="Task", wait_seconds=value)


def test_wait_budget_is_capped_by_configuration() -> None:
    client = HermesClient(settings())

    assert client._bounded_wait(None) == pytest.approx(0.1)
    assert client._bounded_wait(0) == 0
    assert client._bounded_wait(999) == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_api_error_uses_bounded_structured_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "denied"}})

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )

    with pytest.raises(HermesAPIError, match="HTTP 401: denied"):
        await client.health()


@pytest.mark.asyncio
async def test_network_failure_is_normalized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = HermesClient(
        settings(), transport_factory=lambda: httpx.MockTransport(handler)
    )

    with pytest.raises(HermesAPIError, match="Unable to reach"):
        await client.health()
