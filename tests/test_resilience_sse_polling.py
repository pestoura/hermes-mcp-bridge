"""SSE/polling resilience: fallback, idempotency, cancellation, faults."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from faultkit.http import FaultProfile, FaultyTransport, ScriptedResponse, sse_response
from faultkit.sse import (
    duplicated_events,
    invalid_event_stream,
    out_of_order_events,
    truncated_stream,
)

from hermes_mcp_bridge.client import HermesAPIError, HermesClient
from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.models import RunStatus
from hermes_mcp_bridge.observability.metrics import get_metrics, get_registry
from hermes_mcp_bridge.resilience import RunStateTracker, TerminalStateError
from hermes_mcp_bridge.resilience.events import fingerprint

RUN_ID = "run-1"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "hermes_api_key": "test-key",
        "hermes_api_base_url": "http://127.0.0.1:9",
        "hermes_run_poll_interval_seconds": 0.001,
        "hermes_progress_interval_seconds": 0.01,
        "hermes_request_timeout_seconds": 1.0,
        "hermes_event_stream_connect_timeout_seconds": 1.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _client(transport: FaultyTransport, **overrides: object) -> HermesClient:
    return HermesClient(_settings(**overrides), transport_factory=lambda: transport)


def _run(status: str) -> ScriptedResponse:
    return ScriptedResponse(status_code=200, json_body={"run_id": RUN_ID, "status": status})


# -- RunStateTracker idempotency ---------------------------------------


def test_tracker_applies_each_transition_once() -> None:
    tracker = RunStateTracker()
    assert tracker.observe(RUN_ID, "queued").applied is True
    assert tracker.observe(RUN_ID, "running").applied is True
    assert tracker.observe(RUN_ID, "completed").applied is True
    assert tracker.is_terminal(RUN_ID)


def test_duplicate_terminal_event_is_not_double_counted() -> None:
    get_registry().reset()
    tracker = RunStateTracker()
    tracker.observe(RUN_ID, "completed", source="sse")
    for _ in range(5):
        observation = tracker.observe(RUN_ID, "completed", source="polling")
        assert observation.applied is False
        assert observation.reason == "duplicate"
    assert tracker.stats(RUN_ID)["duplicates"] == 5
    assert get_metrics().duplicate_events_total.value(source="polling") == 5.0


def test_out_of_order_event_cannot_regress_a_terminal_state() -> None:
    get_registry().reset()
    tracker = RunStateTracker()
    tracker.observe(RUN_ID, "completed")
    observation = tracker.observe(RUN_ID, "running", source="sse")
    assert observation.applied is False
    assert observation.status == "completed"
    assert get_metrics().out_of_order_events_total.value(source="sse") == 1.0


def test_conflicting_terminal_states_raise() -> None:
    tracker = RunStateTracker()
    tracker.observe(RUN_ID, "completed")
    with pytest.raises(TerminalStateError):
        tracker.observe(RUN_ID, "failed")


def test_lower_sequence_numbers_are_ignored() -> None:
    tracker = RunStateTracker()
    tracker.observe(RUN_ID, "running", sequence=10)
    observation = tracker.observe(RUN_ID, "queued", sequence=3)
    assert observation.applied is False
    assert tracker.status(RUN_ID) == "running"


def test_sse_and_polling_converge_on_one_completion() -> None:
    tracker = RunStateTracker()
    applied = 0
    for source, status in (
        ("sse", "queued"),
        ("polling", "queued"),
        ("sse", "running"),
        ("polling", "running"),
        ("sse", "completed"),
        ("polling", "completed"),
        ("polling", "completed"),
    ):
        if tracker.observe(RUN_ID, status, source=source).applied:
            applied += 1
    assert applied == 3
    assert tracker.stats(RUN_ID)["sources"] == ["polling", "sse"]


def test_forget_releases_tracker_resources() -> None:
    tracker = RunStateTracker()
    tracker.observe(RUN_ID, "running")
    assert tracker.size() == 1
    tracker.forget(RUN_ID)
    assert tracker.size() == 0
    assert tracker.status(RUN_ID) == "unknown"


def test_fingerprint_is_stable_and_non_reversible() -> None:
    value = fingerprint("execution-secret-id")
    assert len(value) == 12
    assert value == fingerprint("execution-secret-id")
    assert "execution" not in value
    assert fingerprint(None) == "none"


# -- client SSE behaviour ----------------------------------------------


async def test_truncated_sse_stream_falls_back_to_polling_without_losing_result() -> None:
    get_registry().reset()
    transport = FaultyTransport(
        script=[
            sse_response(truncated_stream(RUN_ID, keep_chars=30)),
            _run("completed"),
        ],
        default=_run("completed"),
    )
    events: list[dict] = []

    async def progress(event: dict) -> None:
        events.append(event)

    result = await _client(transport).wait_for_run(
        RUN_ID, max_wait_seconds=2.0, progress_callback=progress
    )
    assert result.status is RunStatus.COMPLETED
    assert any(e["event"] == "bridge.event_stream_fallback" for e in events)
    assert get_metrics().sse_fallbacks_total.value(reason="stream_ended") >= 1.0


async def test_invalid_and_unknown_sse_frames_are_ignored() -> None:
    transport = FaultyTransport(
        script=[sse_response(invalid_event_stream(RUN_ID)), _run("completed")],
        default=_run("completed"),
    )
    events: list[dict] = []

    async def progress(event: dict) -> None:
        events.append(event)

    result = await _client(transport).wait_for_run(
        RUN_ID, max_wait_seconds=2.0, progress_callback=progress
    )
    assert result.status is RunStatus.COMPLETED
    assert not any(e.get("event") == "bridge.parse_error" for e in events)


async def test_duplicated_terminal_events_produce_one_final_result() -> None:
    transport = FaultyTransport(
        script=[sse_response(duplicated_events(RUN_ID, repeats=4)), _run("completed")],
        default=_run("completed"),
    )
    tracker = RunStateTracker()
    applied: list[str] = []

    async def progress(event: dict) -> None:
        status = "completed" if event.get("event") == "run.completed" else "running"
        if tracker.observe(RUN_ID, status, source="sse").applied:
            applied.append(status)

    result = await _client(transport).wait_for_run(
        RUN_ID, max_wait_seconds=2.0, progress_callback=progress
    )
    assert result.status is RunStatus.COMPLETED
    assert applied.count("completed") == 1


async def test_out_of_order_sse_events_do_not_regress_state() -> None:
    transport = FaultyTransport(
        script=[sse_response(out_of_order_events(RUN_ID)), _run("completed")],
        default=_run("completed"),
    )
    tracker = RunStateTracker()

    async def progress(event: dict) -> None:
        name = str(event.get("event") or "")
        if name == "run.completed":
            tracker.observe(RUN_ID, "completed", source="sse")
        elif name == "run.started":
            tracker.observe(RUN_ID, "running", source="sse")

    await _client(transport).wait_for_run(RUN_ID, max_wait_seconds=2.0, progress_callback=progress)
    assert tracker.status(RUN_ID) == "completed"


async def test_sse_rejection_status_triggers_polling_fallback() -> None:
    get_registry().reset()
    transport = FaultyTransport(
        script=[
            ScriptedResponse(status_code=503, json_body={"error": {"message": "down"}}),
            _run("running"),
            _run("completed"),
        ],
        default=_run("completed"),
    )
    events: list[dict] = []

    async def progress(event: dict) -> None:
        events.append(event)

    result = await _client(transport).wait_for_run(
        RUN_ID, max_wait_seconds=2.0, progress_callback=progress
    )
    assert result.status is RunStatus.COMPLETED
    assert get_metrics().sse_connections_total.value(outcome="rejected") == 1.0


async def test_connection_reset_during_stream_falls_back() -> None:
    transport = FaultyTransport(
        script=[ScriptedResponse(failure="reset"), _run("completed")],
        default=_run("completed"),
    )
    events: list[dict] = []

    async def progress(event: dict) -> None:
        events.append(event)

    result = await _client(transport).wait_for_run(
        RUN_ID, max_wait_seconds=2.0, progress_callback=progress
    )
    assert result.status is RunStatus.COMPLETED
    assert any(e["event"] == "bridge.event_stream_fallback" for e in events)


async def test_upstream_timeout_is_reported_as_bridge_error() -> None:
    transport = FaultyTransport(default=ScriptedResponse(failure="timeout"))
    with pytest.raises(HermesAPIError, match="timed out"):
        await _client(transport).get_run(RUN_ID)


@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
async def test_upstream_error_statuses_are_surfaced_not_swallowed(status_code: int) -> None:
    transport = FaultyTransport(
        default=ScriptedResponse(
            status_code=status_code,
            json_body={"error": {"message": "boom"}},
            headers={"Retry-After": "1"} if status_code in {429, 503} else {},
        )
    )
    with pytest.raises(HermesAPIError, match=str(status_code)):
        await _client(transport).get_run(RUN_ID)


async def test_polling_records_iterations_and_reaches_terminal_state() -> None:
    get_registry().reset()
    transport = FaultyTransport(
        script=[_run("queued"), _run("running"), _run("completed")],
        default=_run("completed"),
    )
    result = await _client(transport).wait_for_run(RUN_ID, max_wait_seconds=2.0)
    assert result.status is RunStatus.COMPLETED
    assert get_metrics().polling_iterations_total.value() >= 1.0


async def test_cancellation_does_not_mark_success_and_releases_resources() -> None:
    tracker = RunStateTracker()
    tracker.observe(RUN_ID, "running")

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"run_id": RUN_ID, "status": "completed"})

    client = HermesClient(_settings(), transport_factory=lambda: httpx.MockTransport(slow_handler))
    task = asyncio.create_task(client.wait_for_run(RUN_ID, max_wait_seconds=5.0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tracker.status(RUN_ID) == "running"
    assert not tracker.is_terminal(RUN_ID)
    tracker.forget(RUN_ID)
    assert tracker.size() == 0


async def test_cancellation_during_submit_can_request_upstream_stop() -> None:
    stops: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/sessions":
            return httpx.Response(201, json={"session": {"id": "sess-1"}})
        if path == "/v1/runs" and request.method == "POST":
            return httpx.Response(202, json={"run_id": RUN_ID, "status": "queued"})
        if path.endswith("/stop"):
            stops.append(path)
            return httpx.Response(200, json={"run_id": RUN_ID, "status": "cancelled"})
        await asyncio.sleep(5)
        return httpx.Response(200, json={"run_id": RUN_ID, "status": "running"})

    client = HermesClient(_settings(), transport_factory=lambda: httpx.MockTransport(handler))
    task = asyncio.create_task(
        client.submit_prompt(prompt="hello", wait_seconds=5.0, stop_on_cancel=True)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert stops


async def test_fault_profile_is_deterministic_for_a_given_seed() -> None:
    first = FaultProfile(seed=99, status_500_rate=0.5, timeout_rate=0.2)
    second = FaultProfile(seed=99, status_500_rate=0.5, timeout_rate=0.2)
    draws_a = [getattr(first.next_fault(), "status_code", None) for _ in range(20)]
    draws_b = [getattr(second.next_fault(), "status_code", None) for _ in range(20)]
    assert draws_a == draws_b


def test_fault_profile_rejects_rates_over_one() -> None:
    with pytest.raises(ValueError):
        FaultProfile(status_500_rate=0.8, timeout_rate=0.5)
