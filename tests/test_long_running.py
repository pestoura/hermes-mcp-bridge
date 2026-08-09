from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.client import HermesClient
from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.models import HermesPromptResult, RunStatus


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "hermes_api_key": SecretStr("test-key"),
        "hermes_api_base_url": "http://hermes.test",
        "hermes_run_poll_interval_seconds": 0.001,
        "hermes_run_max_wait_seconds": 1.0,
        "hermes_progress_interval_seconds": 0.01,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_connected_stream_reports_events_and_returns_final_status() -> None:
    events: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(201, json={"session": {"id": "session-new"}})
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(
                202,
                json={"run_id": "run-1", "status": "started"},
            )
        if request.method == "GET" and request.url.path.endswith("/events"):
            body = "".join(
                [
                    "data: " + json.dumps({"event": "tool.started", "tool": "github"}) + "\n\n",
                    "data: " + json.dumps({"event": "run.completed", "output": "done"}) + "\n\n",
                    ": stream closed\n\n",
                ]
            )
            return httpx.Response(
                200,
                text=body,
                headers={"content-type": "text/event-stream"},
            )
        if request.method == "GET" and request.url.path == "/v1/runs/run-1":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "session_id": "session-new",
                    "status": "completed",
                    "output": "done",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(),
        transport_factory=lambda: httpx.MockTransport(handler),
    )

    async def progress(event: dict[str, object]) -> None:
        events.append(event)

    result = await client.submit_prompt(
        prompt="Long task",
        progress_callback=progress,
    )

    assert result.status == RunStatus.COMPLETED
    assert result.output == "done"
    assert [event["event"] for event in events] == [
        "bridge.run.accepted",
        "tool.started",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_idle_connected_run_emits_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, object]] = []
    client = HermesClient(settings(hermes_progress_interval_seconds=0.001))

    async def idle_reader(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(60)

    async def completed_run(*args: object, **kwargs: object) -> HermesPromptResult:
        return HermesPromptResult(
            execution_id="run-heartbeat",
            session_id="session-heartbeat",
            status=RunStatus.COMPLETED,
            output="done",
        )

    monkeypatch.setattr(client, "_read_run_events", idle_reader)
    monkeypatch.setattr(client, "get_run", completed_run)

    async def progress(event: dict[str, object]) -> None:
        events.append(event)

    result = await client._wait_for_run_connected(
        "run-heartbeat",
        max_wait_seconds=0.1,
        fallback_session_id="session-heartbeat",
        agent=None,
        subagents=None,
        progress_callback=progress,
    )

    assert result.status == RunStatus.COMPLETED
    assert any(event["event"] == "bridge.heartbeat" for event in events)


@pytest.mark.asyncio
async def test_event_stream_failure_falls_back_to_polling() -> None:
    events: list[dict[str, object]] = []
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(201, json={"session": {"id": "session-new"}})
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(
                202,
                json={"run_id": "run-2", "status": "started"},
            )
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(
                503,
                json={"error": {"message": "stream unavailable"}},
            )
        if request.method == "GET" and request.url.path == "/v1/runs/run-2":
            status_calls += 1
            if status_calls == 1:
                return httpx.Response(
                    200,
                    json={"run_id": "run-2", "status": "running"},
                )
            return httpx.Response(
                200,
                json={
                    "run_id": "run-2",
                    "status": "completed",
                    "output": "ok",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(),
        transport_factory=lambda: httpx.MockTransport(handler),
    )

    async def progress(event: dict[str, object]) -> None:
        events.append(event)

    result = await client.submit_prompt(
        prompt="Long task",
        progress_callback=progress,
    )

    assert result.status == RunStatus.COMPLETED
    assert any(event["event"] == "bridge.event_stream_fallback" for event in events)


def test_default_wait_supports_two_hour_runs() -> None:
    client = HermesClient(
        settings(
            hermes_run_default_wait_seconds=7200.0,
            hermes_run_max_wait_seconds=7200.0,
        )
    )

    assert client._bounded_wait(None) == 7200.0
    assert client._bounded_wait(99_999.0) == 7200.0


@pytest.mark.asyncio
async def test_cancellation_leaves_hermes_running_by_default() -> None:
    stop_requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stop_requested
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(201, json={"session": {"id": "session-new"}})
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(
                202,
                json={"run_id": "run-survives", "status": "started"},
            )
        if request.method == "POST" and request.url.path.endswith("/stop"):
            stop_requested = True
            return httpx.Response(
                200,
                json={"run_id": "run-survives", "status": "stopping"},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(),
        transport_factory=lambda: httpx.MockTransport(handler),
    )

    async def cancelled_wait(*args: object, **kwargs: object) -> object:
        raise asyncio.CancelledError

    client.wait_for_run = cancelled_wait  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await client.submit_prompt(prompt="Long task")

    assert stop_requested is False


@pytest.mark.asyncio
async def test_cancellation_can_explicitly_stop_the_hermes_run() -> None:
    stop_requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stop_requested
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(201, json={"session": {"id": "session-new"}})
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(
                202,
                json={"run_id": "run-cancel", "status": "started"},
            )
        if request.method == "POST" and request.url.path.endswith("/stop"):
            stop_requested = True
            return httpx.Response(
                200,
                json={"run_id": "run-cancel", "status": "stopping"},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(
        settings(),
        transport_factory=lambda: httpx.MockTransport(handler),
    )

    async def cancelled_wait(*args: object, **kwargs: object) -> object:
        raise asyncio.CancelledError

    client.wait_for_run = cancelled_wait  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await client.submit_prompt(
            prompt="Long task",
            stop_on_cancel=True,
        )

    assert stop_requested is True
