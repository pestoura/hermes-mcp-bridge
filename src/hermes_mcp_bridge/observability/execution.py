"""Bounded, in-process execution-efficiency tracking for the 1.x bridge.

An observed execution starts only when ``hermes_prompt`` or ``hermes_submit``
returns a real Hermes execution identifier. Subsequent lifecycle calls are
associated only while that execution remains in this process' bounded tracker.
This deliberately avoids inventing a cross-request/task transaction model.

Execution identifiers are used only as in-memory dictionary keys. They are
never emitted as Prometheus labels or in the sanitized execution summary.
Telemetry is fail-open throughout.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .logging import log_event
from .metrics import get_registry

_START_TOOLS = frozenset({"hermes_prompt", "hermes_submit"})
_LIFECYCLE_TOOLS = frozenset(
    {
        "hermes_prompt",
        "hermes_submit",
        "hermes_wait",
        "hermes_status",
        "hermes_stop",
    }
)
_TERMINAL_OUTCOMES = {
    "completed": "success",
    "failed": "failed",
    "cancelled": "cancelled",
}
_MAX_TRACKED_EXECUTIONS = 4096


@dataclass
class ExecutionCallStats:
    """Per-MCP-call counters accumulated by low-level instrumentation."""

    tool_name: str
    started_at: float = field(default_factory=time.perf_counter)
    upstream_calls: int = 0
    upstream_duration_seconds: float = 0.0
    poll_iterations: int = 0
    retries: int = 0
    recoveries: int = 0
    poll_wait_seconds: float = 0.0
    sse_wait_seconds: float = 0.0


@dataclass
class _ExecutionStats:
    started_at: float
    last_seen_at: float
    tool_calls: int = 0
    upstream_calls: int = 0
    upstream_duration_seconds: float = 0.0
    poll_iterations: int = 0
    retries: int = 0
    recoveries: int = 0
    poll_wait_seconds: float = 0.0
    sse_wait_seconds: float = 0.0


_current_call: ContextVar[ExecutionCallStats | None] = ContextVar(
    "bridge_execution_call_stats", default=None
)
_lock = threading.RLock()
_tracked: "OrderedDict[str, _ExecutionStats]" = OrderedDict()


def _metric_histogram(name: str, help_text: str, buckets: tuple[float, ...] | None = None) -> Any:
    registry = get_registry()
    if buckets is None:
        return registry.histogram(name, help_text)
    return registry.histogram(name, help_text, buckets=buckets)


def _metric_counter(name: str, help_text: str) -> Any:
    return get_registry().counter(name, help_text)


def _observability_error() -> None:
    with suppress(Exception):
        get_registry().counter(
            "bridge_observability_errors_total",
            "Observability internal failures by kind.",
        ).inc(kind="instrumentation")


def _normalize_execution_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text == "not-created" or len(text) > 128:
        return None
    return text


def _extract_result_value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)


def _resolve_execution_id(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], result: Any
) -> str | None:
    direct = _normalize_execution_id(_extract_result_value(result, "execution_id"))
    if direct:
        return direct
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
    except Exception:
        return _normalize_execution_id(kwargs.get("execution_id"))
    return _normalize_execution_id(bound.arguments.get("execution_id"))


def _status(result: Any) -> str:
    raw = _extract_result_value(result, "status")
    value = getattr(raw, "value", raw)
    return str(value or "unknown").strip().lower()


def _prune_locked() -> None:
    while len(_tracked) > _MAX_TRACKED_EXECUTIONS:
        _tracked.popitem(last=False)


def _merge_call(stats: _ExecutionStats, call: ExecutionCallStats) -> None:
    stats.tool_calls += 1
    stats.upstream_calls += max(0, int(call.upstream_calls))
    stats.upstream_duration_seconds += max(0.0, float(call.upstream_duration_seconds))
    stats.poll_iterations += max(0, int(call.poll_iterations))
    stats.retries += max(0, int(call.retries))
    stats.recoveries += max(0, int(call.recoveries))
    stats.poll_wait_seconds += max(0.0, float(call.poll_wait_seconds))
    stats.sse_wait_seconds += max(0.0, float(call.sse_wait_seconds))
    stats.last_seen_at = time.perf_counter()


def _emit_terminal(stats: _ExecutionStats, *, outcome: str) -> None:
    duration = max(0.0, time.perf_counter() - stats.started_at)
    try:
        count_buckets = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0, 55.0, 100.0)
        _metric_histogram(
            "bridge_execution_duration_seconds",
            "Observed Hermes execution lifecycle duration in seconds, by terminal outcome.",
        ).observe(duration, outcome=outcome)
        _metric_histogram(
            "bridge_execution_tool_calls",
            "Observed MCP lifecycle calls per terminal Hermes execution.",
            count_buckets,
        ).observe(float(stats.tool_calls), outcome=outcome)
        _metric_histogram(
            "bridge_execution_upstream_calls",
            "Observed upstream Hermes API calls per terminal Hermes execution.",
            count_buckets,
        ).observe(float(stats.upstream_calls), outcome=outcome)
        _metric_histogram(
            "bridge_execution_poll_iterations",
            "Observed polling iterations per terminal Hermes execution.",
            count_buckets,
        ).observe(float(stats.poll_iterations), outcome=outcome)
        _metric_histogram(
            "bridge_execution_retries",
            "Observed safe retries per terminal Hermes execution.",
            count_buckets,
        ).observe(float(stats.retries), outcome=outcome)
        _metric_histogram(
            "bridge_execution_recoveries",
            "Observed recoveries per terminal Hermes execution.",
            count_buckets,
        ).observe(float(stats.recoveries), outcome=outcome)
        _metric_counter(
            "bridge_execution_terminal_total",
            "Terminal Hermes executions observed by the bridge, by outcome.",
        ).inc(outcome=outcome)
        log_event(
            "bridge.execution.summary",
            outcome=outcome,
            duration_ms=round(duration * 1000, 3),
            tool_calls=stats.tool_calls,
            upstream_calls=stats.upstream_calls,
            upstream_duration_ms=round(stats.upstream_duration_seconds * 1000, 3),
            poll_iterations=stats.poll_iterations,
            retries=stats.retries,
            recoveries=stats.recoveries,
            poll_wait_ms=round(stats.poll_wait_seconds * 1000, 3),
            sse_wait_ms=round(stats.sse_wait_seconds * 1000, 3),
        )
    except Exception:
        _observability_error()


@contextmanager
def execution_call_scope(tool_name: str) -> Iterator[ExecutionCallStats]:
    """Collect low-level counters for one MCP call without changing semantics."""

    stats = ExecutionCallStats(tool_name=str(tool_name))
    token: Token[ExecutionCallStats | None] = _current_call.set(stats)
    try:
        yield stats
    finally:
        _current_call.reset(token)


def complete_execution_call(
    *,
    tool_name: str,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    result: Any,
    call_stats: ExecutionCallStats,
) -> None:
    """Merge one lifecycle tool call and emit a terminal summary exactly once.

    A terminal result is ignored when the run was not already being observed by
    this process and the current tool is not a starter. This is intentional: it
    prevents a status lookup of an old terminal run from fabricating a new task.
    """

    if tool_name not in _LIFECYCLE_TOOLS:
        return
    execution_id = _resolve_execution_id(func, args, kwargs, result)
    if execution_id is None:
        return
    terminal_outcome = _TERMINAL_OUTCOMES.get(_status(result))

    terminal_stats: _ExecutionStats | None = None
    with _lock:
        stats = _tracked.get(execution_id)
        if stats is None:
            if tool_name not in _START_TOOLS:
                return
            stats = _ExecutionStats(
                started_at=call_stats.started_at,
                last_seen_at=time.perf_counter(),
            )
            _tracked[execution_id] = stats
            _prune_locked()
        else:
            _tracked.move_to_end(execution_id)
        _merge_call(stats, call_stats)
        if terminal_outcome is not None:
            terminal_stats = _tracked.pop(execution_id, None)

    if terminal_stats is not None:
        _emit_terminal(terminal_stats, outcome=terminal_outcome)


def observe_upstream_call(duration_seconds: float) -> None:
    stats = _current_call.get()
    if stats is None:
        return
    stats.upstream_calls += 1
    stats.upstream_duration_seconds += max(0.0, float(duration_seconds))


def observe_poll_iteration() -> None:
    stats = _current_call.get()
    if stats is not None:
        stats.poll_iterations += 1


def observe_retry() -> None:
    stats = _current_call.get()
    if stats is not None:
        stats.retries += 1


def observe_recovery(count: int = 1) -> None:
    stats = _current_call.get()
    if stats is not None:
        stats.recoveries += max(0, int(count))


def observe_poll_wait(seconds: float) -> None:
    amount = max(0.0, float(seconds))
    stats = _current_call.get()
    if stats is not None:
        stats.poll_wait_seconds += amount
    try:
        _metric_histogram(
            "bridge_poll_wait_seconds",
            "Actual time spent sleeping between run polling attempts.",
        ).observe(amount)
    except Exception:
        _observability_error()


def observe_sse_wait(seconds: float) -> None:
    amount = max(0.0, float(seconds))
    stats = _current_call.get()
    if stats is not None:
        stats.sse_wait_seconds += amount
    try:
        _metric_histogram(
            "bridge_sse_wait_seconds",
            "Actual time spent waiting for SSE run events or heartbeat timeout.",
        ).observe(amount)
    except Exception:
        _observability_error()


def observe_serialization(seconds: float) -> None:
    amount = max(0.0, float(seconds))
    try:
        _metric_histogram(
            "bridge_serialization_duration_seconds",
            "JSON decoding/serialization boundary duration in seconds.",
        ).observe(amount)
    except Exception:
        _observability_error()


def reset_execution_tracking() -> None:
    """Test/maintenance helper; does not reset Prometheus metrics."""

    with _lock:
        _tracked.clear()
