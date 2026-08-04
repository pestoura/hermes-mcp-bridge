"""Central instrumentation wrappers.

A single decorator instruments every MCP tool (no need to hand-edit 26
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
                _call("tool_inflight", "inc", tool=tool_name)
                try:
                    with correlation_scope(
                        correlation_id=new_correlation_id(), tool_name=tool_name
                    ), start_span(f"tool.{tool_name}"):
                        return await func(*args, **kwargs)
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                except Exception:
                    outcome = "error"
                    raise
                finally:
                    _finish_tool(tool_name, started, outcome)

            return awrapper

        @functools.wraps(func)
        def swrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            outcome = "success"
            _call("tool_inflight", "inc", tool=tool_name)
            try:
                with correlation_scope(
                    correlation_id=new_correlation_id(), tool_name=tool_name
                ), start_span(f"tool.{tool_name}"):
                    return func(*args, **kwargs)
            except Exception:
                outcome = "error"
                raise
            finally:
                _finish_tool(tool_name, started, outcome)

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
