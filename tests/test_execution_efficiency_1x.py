"""Regression gates for 1.x execution-efficiency observability."""

from __future__ import annotations

import httpx

from hermes_mcp_bridge import client as client_module
from hermes_mcp_bridge.observability import execution
from hermes_mcp_bridge.observability.metrics import get_registry, render_prometheus


def _dummy_prompt() -> None:
    return None


def _dummy_status(execution_id: str) -> None:
    del execution_id


def setup_function() -> None:
    get_registry().reset()
    execution.reset_execution_tracking()


def test_execution_summary_aggregates_only_observed_lifecycle(monkeypatch) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        execution,
        "log_event",
        lambda event, **fields: events.append((event, fields)),
    )

    with execution.execution_call_scope("hermes_submit") as first:
        execution.observe_upstream_call(0.2)
        execution.observe_upstream_call(0.3)
        execution.observe_poll_iteration()
        execution.observe_poll_wait(0.05)
        execution.complete_execution_call(
            tool_name="hermes_submit",
            func=_dummy_prompt,
            args=(),
            kwargs={},
            result={"execution_id": "run-secret-1", "status": "running"},
            call_stats=first,
        )

    # A later lifecycle call is associated only because the run is already
    # being observed by this process.
    with execution.execution_call_scope("hermes_status") as second:
        execution.observe_upstream_call(0.4)
        execution.observe_poll_iteration()
        execution.observe_retry()
        execution.observe_sse_wait(0.07)
        execution.complete_execution_call(
            tool_name="hermes_status",
            func=_dummy_status,
            args=("run-secret-1",),
            kwargs={},
            result={"execution_id": "run-secret-1", "status": "completed"},
            call_stats=second,
        )

    registry = get_registry()
    assert registry.histogram(
        "bridge_execution_tool_calls", "unused"
    ).snapshot(outcome="success") == {"count": 1, "sum": 2.0}
    assert registry.histogram(
        "bridge_execution_upstream_calls", "unused"
    ).snapshot(outcome="success") == {"count": 1, "sum": 3.0}
    assert registry.histogram(
        "bridge_execution_poll_iterations", "unused"
    ).snapshot(outcome="success") == {"count": 1, "sum": 2.0}
    assert registry.histogram(
        "bridge_execution_retries", "unused"
    ).snapshot(outcome="success") == {"count": 1, "sum": 1.0}
    assert registry.counter(
        "bridge_execution_terminal_total", "unused"
    ).value(outcome="success") == 1.0

    summaries = [fields for event, fields in events if event == "bridge.execution.summary"]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["tool_calls"] == 2
    assert summary["upstream_calls"] == 3
    assert summary["poll_iterations"] == 2
    assert summary["retries"] == 1
    rendered = repr(summary)
    assert "run-secret-1" not in rendered
    assert "execution_id" not in rendered
    assert "prompt" not in rendered
    assert "output" not in rendered


def test_old_terminal_status_does_not_fabricate_task_summary(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(execution, "log_event", lambda event, **_fields: events.append(event))

    with execution.execution_call_scope("hermes_status") as stats:
        execution.observe_upstream_call(0.1)
        execution.complete_execution_call(
            tool_name="hermes_status",
            func=_dummy_status,
            args=("old-run",),
            kwargs={},
            result={"execution_id": "old-run", "status": "completed"},
            call_stats=stats,
        )

    assert "bridge.execution.summary" not in events
    assert get_registry().counter(
        "bridge_execution_terminal_total", "unused"
    ).value(outcome="success") == 0.0


def test_failed_started_execution_has_bounded_terminal_outcome() -> None:
    with execution.execution_call_scope("hermes_prompt") as stats:
        execution.complete_execution_call(
            tool_name="hermes_prompt",
            func=_dummy_prompt,
            args=(),
            kwargs={},
            result={"execution_id": "run-failed", "status": "failed"},
            call_stats=stats,
        )

    text = render_prometheus()
    assert 'bridge_execution_terminal_total{outcome="failed"} 1' in text
    assert "run-failed" not in text


def test_internal_wait_metrics_use_no_labels_or_identifiers() -> None:
    with execution.execution_call_scope("hermes_wait"):
        execution.observe_poll_wait(0.12)
        execution.observe_sse_wait(0.34)
        execution.observe_serialization(0.005)

    text = render_prometheus()
    assert "bridge_poll_wait_seconds_count 1" in text
    assert "bridge_sse_wait_seconds_count 1" in text
    assert "bridge_serialization_duration_seconds_count 1" in text
    assert "execution_id=" not in text


def test_client_decode_records_real_serialization_boundary() -> None:
    response = httpx.Response(200, json={"status": "ok"})
    payload = client_module.HermesClient._decode(response, expected={200})
    assert payload == {"status": "ok"}
    snapshot = get_registry().histogram(
        "bridge_serialization_duration_seconds", "unused"
    ).snapshot()
    assert snapshot["count"] == 1
    assert snapshot["sum"] >= 0.0
