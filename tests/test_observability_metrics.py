"""Metrics registry, cardinality policy and instrumentation accounting."""

from __future__ import annotations

import asyncio
import threading

import pytest

from hermes_mcp_bridge.observability import instrumentation as inst
from hermes_mcp_bridge.observability.metrics import (
    ALLOWED_LABELS,
    FORBIDDEN_LABELS,
    MAX_SERIES_PER_METRIC,
    CardinalityError,
    MetricsRegistry,
    get_metrics,
    get_registry,
    render_prometheus,
    set_bridge_info,
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    get_registry().reset()
    yield
    get_registry().reset()


def test_counter_increments_by_labels() -> None:
    m = get_metrics()
    m.tool_calls_total.inc(tool="hermes_prompt", outcome="success")
    m.tool_calls_total.inc(tool="hermes_prompt", outcome="success")
    m.tool_calls_total.inc(tool="hermes_prompt", outcome="error")
    assert m.tool_calls_total.value(tool="hermes_prompt", outcome="success") == 2
    assert m.tool_calls_total.value(tool="hermes_prompt", outcome="error") == 1


def test_gauge_inc_dec_and_set() -> None:
    m = get_metrics()
    m.tool_inflight.inc(tool="t")
    m.tool_inflight.inc(tool="t")
    m.tool_inflight.dec(tool="t")
    assert m.tool_inflight.value(tool="t") == 1
    m.active_runs.set(7)
    assert m.active_runs.value() == 7


def test_histogram_counts_and_sums() -> None:
    m = get_metrics()
    m.tool_duration_seconds.observe(0.2, tool="t")
    m.tool_duration_seconds.observe(1.5, tool="t")
    snap = m.tool_duration_seconds.snapshot(tool="t")
    assert snap["count"] == 2
    assert snap["sum"] == pytest.approx(1.7)


@pytest.mark.parametrize("label", sorted(FORBIDDEN_LABELS))
def test_forbidden_labels_are_rejected(label: str) -> None:
    with pytest.raises(CardinalityError):
        get_metrics().tool_calls_total.inc(**{label: "x"})


def test_unknown_labels_are_rejected() -> None:
    with pytest.raises(CardinalityError):
        get_metrics().tool_calls_total.inc(whatever="x")


def test_no_high_cardinality_labels_in_registry_after_use() -> None:
    m = get_metrics()
    m.tool_calls_total.inc(tool="hermes_prompt", outcome="success")
    inst.record_upstream(
        path="/v1/runs/abc", status_code=200, duration_seconds=0.1, outcome="success"
    )
    inst.record_sse_fallback("stream closed")
    inst.record_approval("approved")
    inst.record_sqlite_error("database is locked")
    used = get_registry().label_names()
    assert used <= ALLOWED_LABELS
    assert not used & FORBIDDEN_LABELS


def test_series_cap_prevents_unbounded_growth() -> None:
    counter = get_metrics().tool_calls_total
    for i in range(MAX_SERIES_PER_METRIC + 50):
        counter.inc(tool=f"tool{i}", outcome="success")
    assert len(counter._values) <= MAX_SERIES_PER_METRIC


def test_registry_is_thread_safe() -> None:
    counter = get_metrics().tool_calls_total

    def work() -> None:
        for _ in range(500):
            counter.inc(tool="t", outcome="success")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert counter.value(tool="t", outcome="success") == 4000


def test_prometheus_output_is_well_formed() -> None:
    m = get_metrics()
    m.tool_calls_total.inc(tool="hermes_prompt", outcome="success")
    m.tool_duration_seconds.observe(0.3, tool="hermes_prompt")
    set_bridge_info("0.8.0")
    text = render_prometheus()
    assert "# HELP bridge_tool_calls_total" in text
    assert "# TYPE bridge_tool_calls_total counter" in text
    assert 'bridge_tool_calls_total{outcome="success",tool="hermes_prompt"} 1.0' in text
    assert "bridge_tool_duration_seconds_bucket" in text
    assert 'le="+Inf"' in text
    assert "bridge_tool_duration_seconds_count" in text
    assert 'bridge_info{version="0.8.0"} 1.0' in text
    assert text.endswith("\n")
    for line in text.splitlines():
        assert line.startswith("#") or " " in line


def test_endpoint_and_status_classification() -> None:
    assert inst.endpoint_class("/v1/runs") == "runs"
    assert inst.endpoint_class("/v1/runs/abc123/events") == "run_events"
    assert inst.endpoint_class("/v1/runs/abc123/stop") == "run_stop"
    assert inst.endpoint_class("/api/sessions") == "sessions"
    assert inst.endpoint_class("/health/detailed") == "health"
    assert inst.endpoint_class("/weird") == "other"
    assert inst.status_class(200) == "2xx"
    assert inst.status_class(503) == "5xx"
    assert inst.status_class(None) == "error"
    assert inst.status_class(999) == "error"


def test_upstream_metrics_recorded() -> None:
    inst.record_upstream(
        path="/v1/runs", status_code=503, duration_seconds=0.4, outcome="upstream_error"
    )
    m = get_metrics()
    assert m.upstream_requests_total.value(endpoint_class="runs", status_class="5xx") == 1
    assert m.upstream_duration_seconds.snapshot(endpoint_class="runs")["count"] == 1


def test_sse_fallback_and_polling_metrics() -> None:
    inst.record_sse_connection("open")
    inst.record_sse_fallback("stream ended")
    inst.record_polling_iteration()
    inst.record_polling_iteration()
    m = get_metrics()
    assert m.sse_connections_total.value(outcome="open") == 1
    assert m.sse_fallbacks_total.value(reason="stream_ended") == 1
    assert m.polling_iterations_total.value() == 2


def test_sqlite_lock_contention_is_derived() -> None:
    inst.record_sqlite_error("database is locked")
    m = get_metrics()
    assert m.sqlite_errors_total.value(kind="database is locked") == 1
    assert m.sqlite_lock_contention_total.value() == 1


async def test_instrument_tool_success_error_and_cancel() -> None:
    @inst.instrument_tool("demo")
    async def ok() -> str:
        return "fine"

    @inst.instrument_tool("demo")
    async def boom() -> str:
        raise RuntimeError("bad")

    @inst.instrument_tool("demo")
    async def cancelled() -> str:
        raise asyncio.CancelledError

    assert await ok() == "fine"
    with pytest.raises(RuntimeError):
        await boom()
    with pytest.raises(asyncio.CancelledError):
        await cancelled()

    m = get_metrics()
    assert m.tool_calls_total.value(tool="demo", outcome="success") == 1
    assert m.tool_calls_total.value(tool="demo", outcome="error") == 1
    assert m.tool_calls_total.value(tool="demo", outcome="cancelled") == 1
    assert m.tool_inflight.value(tool="demo") == 0
    assert m.tool_duration_seconds.snapshot(tool="demo")["count"] == 3


async def test_instrument_tool_retry_accounting() -> None:
    attempts = {"n": 0}

    @inst.instrument_tool("retrying")
    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await flaky()
    assert await flaky() == "ok"
    m = get_metrics()
    assert m.tool_calls_total.value(tool="retrying", outcome="error") == 2
    assert m.tool_calls_total.value(tool="retrying", outcome="success") == 1


async def test_instrument_tool_concurrency_inflight_returns_to_zero() -> None:
    started = asyncio.Event()

    @inst.instrument_tool("conc")
    async def slow() -> None:
        started.set()
        await asyncio.sleep(0.02)

    await asyncio.gather(*(slow() for _ in range(10)))
    m = get_metrics()
    assert m.tool_calls_total.value(tool="conc", outcome="success") == 10
    assert m.tool_inflight.value(tool="conc") == 0


async def test_metrics_failure_does_not_break_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def __getattr__(self, _name: str):
            raise RuntimeError("registry exploded")

    monkeypatch.setattr(inst, "get_metrics", lambda: Broken())

    @inst.instrument_tool("resilient")
    async def work() -> str:
        return "still works"

    assert await work() == "still works"


async def test_logging_failure_does_not_break_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_a, **_k):
        raise RuntimeError("logging exploded")

    monkeypatch.setattr(inst, "log_event", explode)

    @inst.instrument_tool("resilient2")
    async def work() -> str:
        return "value"

    assert await work() == "value"


def test_isolated_registry_instances_do_not_share_state() -> None:
    other = MetricsRegistry()
    counter = other.counter("x_total", "help")
    counter.inc(tool="a")
    assert counter.value(tool="a") == 1
    assert "x_total" not in render_prometheus()
