"""Central instrumentation wrappers.

A single decorator instruments every MCP tool (no need to hand-edit 27
functions), plus helpers for upstream requests and SSE→polling transitions.
All instrumentation is fail-open: a telemetry error can never change the result
of the wrapped call.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from .context import correlation_scope, new_correlation_id
from .execution import (
    complete_execution_call,
    execution_call_scope,
    observe_poll_iteration,
    observe_recovery,
    observe_upstream_call,
)
from .logging import log_event
from .metrics import get_metrics
from .tracing import start_span

_ENDPOINT_CLASSES: tuple[tuple[str, str], ...] = (
    ("/v1/runs", "runs"),
    ("/api/sessions", "sessions"),
    ("/health", "health"),
)


def endpoint_class(path: str) -> str:
    """Map a request path to a low-cardinality endpoint class."""

    text = str(path or "")
    if "/events" in text:
        return "run_events"
    if text.startswith("/v1/runs") and text.rstrip("/").endswith("/stop"):
        return "run_stop"
    for prefix, name in _ENDPOINT_CLASSES:
        if text.startswith(prefix):
            return name
    return "other"


def status_class(status_code: int | None) -> str:
    if status_code is None:
        return "error"
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return "error"
    if code < 100 or code > 599:
        return "error"
    return f"{code // 100}xx"


def _safe(fn: Callable[..., Any] | None, *args: Any, **kwargs: Any) -> None:
    try:
        if fn is None:
            return
        fn(*args, **kwargs)
    except Exception:
        with suppress(Exception):
            get_metrics().observability_errors_total.inc(kind="instrumentation")


def _metric(name: str) -> Any | None:
    """Fetch a metric object without ever raising."""

    try:
        return getattr(get_metrics(), name)
    except Exception:
        return None


def _call(metric_name: str, method: str, *args: Any, **kwargs: Any) -> None:
    metric = _metric(metric_name)
    if metric is None:
        return
    _safe(getattr(metric, method, None), *args, **kwargs)


def record_upstream(
    *, path: str, status_code: int | None, duration_seconds: float, outcome: str
) -> None:
    """Record one upstream Hermes API call."""

    ec = endpoint_class(path)
    sc = status_class(status_code)
    _call("upstream_requests_total", "inc", endpoint_class=ec, status_class=sc)
    _call("upstream_duration_seconds", "observe", duration_seconds, endpoint_class=ec)
    _safe(observe_upstream_call, duration_seconds)
    _safe(
        log_event,
        "bridge.upstream.request",
        endpoint_class=ec,
        status_class=sc,
        outcome=outcome,
        duration_ms=round(duration_seconds * 1000, 3),
    )


def record_sse_connection(outcome: str) -> None:
    _call("sse_connections_total", "inc", outcome=str(outcome)[:32])


def record_sse_fallback(reason: str) -> None:
    normalized = (reason or "unknown").strip().lower().replace(" ", "_")[:32]
    _call("sse_fallbacks_total", "inc", reason=normalized)
    _safe(log_event, "bridge.sse.fallback", reason=normalized, outcome="fallback")


def record_polling_iteration() -> None:
    _call("polling_iterations_total", "inc")
    _safe(observe_poll_iteration)


def record_approval(decision: str) -> None:
    _call("approvals_total", "inc", decision=str(decision)[:32])


def record_sqlite_error(kind: str) -> None:
    normalized = (kind or "unknown").strip().lower()[:32]
    _call("sqlite_errors_total", "inc", kind=normalized)
    if "lock" in normalized or "busy" in normalized:
        _call("sqlite_lock_contention_total", "inc")


def record_sqlite_operation(*, kind: str, outcome: str, exc: BaseException | None = None) -> None:
    """Record a real SQLite operation outcome without double-counting.

    ``kind`` is a low-cardinality operation class (state, approvals, locks,
    migrations) and ``outcome`` one of ``success``/``error``. On error, a
    normalized SQLite kind label is emitted and lock contention is derived when
    the exception message mentions a locked/busy database.
    """

    resolved_kind = (kind or "other").strip().lower()[:32]
    if outcome == "error" or exc is not None:
        message = str(getattr(exc, "args", ("",))[0] if exc is not None else "") or ""
        if "locked" in message.lower() or "busy" in message.lower():
            record_sqlite_error(f"{resolved_kind} lock")
        else:
            record_sqlite_error(resolved_kind)
        _safe(log_event, "bridge.sqlite.error", kind=resolved_kind, outcome="error")
    else:
        _safe(log_event, "bridge.sqlite.ok", kind=resolved_kind, outcome="success")


_UPSTREAM_CLASSES = frozenset(
    {"runs", "run_events", "run_stop", "sessions", "health", "other"}
)
_EVENT_SOURCES = frozenset({"sse", "polling", "recovery", "unknown", "other"})
_CIRCUIT_STATES = frozenset({"closed", "open", "half_open", "other"})


def _bounded(value: str | None, allowed: frozenset[str]) -> str:
    normalized = (value or "other").strip().lower().replace("-", "_")[:32]
    return normalized if normalized in allowed else "other"


def record_sqlite_retry(*, kind: str) -> None:
    """One bounded retry after transient SQLite contention."""

    normalized = (kind or "other").strip().lower()[:32]
    _call("sqlite_retries_total", "inc", kind=normalized)
    _call("sqlite_lock_contention_total", "inc")
    _safe(log_event, "bridge.sqlite.retry", kind=normalized, outcome="retry")


def record_circuit_transition(*, name: str, state: str) -> None:
    upstream = _bounded(name, _UPSTREAM_CLASSES)
    target = _bounded(state, _CIRCUIT_STATES)
    _call("circuit_transitions_total", "inc", upstream=upstream, state=target)
    _safe(
        log_event,
        "bridge.circuit.transition",
        upstream=upstream,
        state=target,
        outcome="transition",
    )


def record_circuit_rejection(*, name: str) -> None:
    upstream = _bounded(name, _UPSTREAM_CLASSES)
    _call("circuit_rejections_total", "inc", upstream=upstream)
    _safe(log_event, "bridge.circuit.rejected", upstream=upstream, outcome="rejected")


def record_duplicate_event(*, source: str) -> None:
    origin = _bounded(source, _EVENT_SOURCES)
    _call("duplicate_events_total", "inc", source=origin)


def record_out_of_order_event(*, source: str) -> None:
    origin = _bounded(source, _EVENT_SOURCES)
    _call("out_of_order_events_total", "inc", source=origin)


def record_backoff_sleep(seconds: float, *, source: str = "unknown") -> None:
    origin = _bounded(source, _EVENT_SOURCES)
    _call("backoff_sleep_seconds", "observe", float(seconds), source=origin)


def record_recovery(*, outcome: str, count: int = 1) -> None:
    """Runs recovered from persisted state after a restart."""

    normalized = (outcome or "other").strip().lower()[:32]
    bounded_count = max(0, int(count))
    _call("recovery_runs_total", "inc", float(bounded_count), outcome=normalized)
    _safe(observe_recovery, bounded_count)
    _safe(log_event, "bridge.recovery", outcome=normalized)


def set_active_runs(count: int) -> None:
    _call("active_runs", "set", float(count))


def set_migrations_version(version: int | float) -> None:
    _call("migrations_version", "set", float(version))


def instrument_tool(
    name: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Instrument an MCP tool with logs, metrics, span and correlation.

    Supports both async and sync functions while preserving signature and
    return type. Async generator tools (streaming) are NOT wrapped: instrumentation
    would consume the stream, so the original function is returned unchanged with a
    warning event. This keeps streaming tools safe by construction.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or getattr(func, "__name__", "tool")

        import inspect

        if inspect.isasyncgenfunction(func) or inspect.isgeneratorfunction(func):
            _safe(
                log_event,
                "bridge.tool.skip_instrumentation",
                tool=tool_name,
                outcome="unsupported_generator",
            )
            return func

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter()
                outcome = "success"
                result: Any = None
                call_stats: Any = None
                _call("tool_inflight", "inc", tool=tool_name)
                try:
                    with (
                        correlation_scope(
                            correlation_id=new_correlation_id(), tool_name=tool_name
                        ),
                        execution_call_scope(tool_name) as scoped_stats,
                        start_span(f"tool.{tool_name}"),
                    ):
                        call_stats = scoped_stats
                        result = await func(*args, **kwargs)
                        return result
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                except Exception:
                    outcome = "error"
                    raise
                finally:
                    _finish_tool(tool_name, started, outcome)
                    if outcome == "success" and call_stats is not None:
                        _safe(
                            complete_execution_call,
                            tool_name=tool_name,
                            func=func,
                            args=args,
                            kwargs=kwargs,
                            result=result,
                            call_stats=call_stats,
                        )

            return awrapper

        @functools.wraps(func)
        def swrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            outcome = "success"
            result: Any = None
            call_stats: Any = None
            _call("tool_inflight", "inc", tool=tool_name)
            try:
                with (
                    correlation_scope(
                        correlation_id=new_correlation_id(), tool_name=tool_name
                    ),
                    execution_call_scope(tool_name) as scoped_stats,
                    start_span(f"tool.{tool_name}"),
                ):
                    call_stats = scoped_stats
                    result = func(*args, **kwargs)
                    return result
            except Exception:
                outcome = "error"
                raise
            finally:
                _finish_tool(tool_name, started, outcome)
                if outcome == "success" and call_stats is not None:
                    _safe(
                        complete_execution_call,
                        tool_name=tool_name,
                        func=func,
                        args=args,
                        kwargs=kwargs,
                        result=result,
                        call_stats=call_stats,
                    )

        return swrapper

    return decorator


def _finish_tool(tool_name: str, started: float, outcome: str) -> None:
    duration = time.perf_counter() - started
    _call("tool_inflight", "dec", tool=tool_name)
    _call("tool_calls_total", "inc", tool=tool_name, outcome=outcome)
    _call("tool_duration_seconds", "observe", duration, tool=tool_name)
    _safe(
        log_event,
        "bridge.tool.call",
        tool=tool_name,
        outcome=outcome,
        duration_ms=round(duration * 1000, 3),
    )


def instrument_all_tools(mcp_server: Any) -> int:
    """Wrap every already-registered FastMCP tool function in place.

    Returns the number of instrumented tools. Fail-open: any structural change
    upstream simply results in zero instrumented tools, never an exception.
    """

    count = 0
    try:
        manager = mcp_server._tool_manager
        tools = getattr(manager, "_tools", None)
        if not isinstance(tools, dict):
            return 0
        for tool_name, tool in tools.items():
            fn = getattr(tool, "fn", None)
            if fn is None or getattr(fn, "__bridge_instrumented__", False):
                continue
            wrapped = instrument_tool(tool_name)(fn)
            wrapped.__bridge_instrumented__ = True  # type: ignore[attr-defined]
            try:
                object.__setattr__(tool, "fn", wrapped)
            except Exception:
                continue
            count += 1
    except Exception:
        return count
    return count
