"""Optional tracing: a no-op span interface with W3C traceparent propagation.

OpenTelemetry is *never* required to boot. When ``BRIDGE_TRACING_ENABLED=1`` and
the ``opentelemetry-api`` package is importable, real spans are created;
otherwise the no-op implementation is used. Telemetry failures are fail-open —
this must never be used for auth or policy decisions.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .context import correlation_scope, get_field

ENV_TRACING_ENABLED = "BRIDGE_TRACING_ENABLED"
ENV_TRACING_EXPORT = "BRIDGE_TRACING_EXPORT"

TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})"
    r"-(?P<trace_id>[0-9a-f]{32})"
    r"-(?P<span_id>[0-9a-f]{16})"
    r"-(?P<flags>[0-9a-f]{2})$"
)

_INVALID_TRACE_ID = "0" * 32
_INVALID_SPAN_ID = "0" * 16


def parse_traceparent(value: Any) -> dict[str, str] | None:
    """Parse a W3C traceparent header; return ``None`` when invalid."""

    if not isinstance(value, str):
        return None
    match = TRACEPARENT_RE.fullmatch(value.strip())
    if not match:
        return None
    if match.group("version") == "ff":
        return None
    if match.group("trace_id") == _INVALID_TRACE_ID:
        return None
    if match.group("span_id") == _INVALID_SPAN_ID:
        return None
    return {
        "version": match.group("version"),
        "trace_id": match.group("trace_id"),
        "span_id": match.group("span_id"),
        "trace_flags": match.group("flags"),
    }


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def format_traceparent(trace_id: str, span_id: str, flags: str = "01") -> str:
    return f"00-{trace_id}-{span_id}-{flags}"


class NoOpSpan:
    """Span object with the minimal interface used by the bridge."""

    __slots__ = ("_delegate", "attributes", "name", "span_id", "trace_id")

    def __init__(self, name: str, trace_id: str, span_id: str, delegate: Any = None) -> None:
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.attributes: dict[str, Any] = {}
        self._delegate = delegate

    def set_attribute(self, key: str, value: Any) -> None:
        try:
            self.attributes[str(key)] = value
            if self._delegate is not None:
                self._delegate.set_attribute(str(key), value)
        except Exception:  # pragma: no cover - fail open
            pass

    def traceparent(self) -> str:
        return format_traceparent(self.trace_id, self.span_id)


def tracing_enabled() -> bool:
    return os.environ.get(ENV_TRACING_ENABLED, "").strip().lower() in {"1", "true", "yes", "on"}


def export_enabled() -> bool:
    """Export is disabled by default."""

    return os.environ.get(ENV_TRACING_EXPORT, "").strip().lower() in {"1", "true", "yes", "on"}


def _otel_tracer() -> Any | None:
    if not tracing_enabled():
        return None
    try:  # pragma: no cover - optional dependency
        from opentelemetry import trace as otel_trace

        return otel_trace.get_tracer("hermes-mcp-bridge")
    except Exception:
        return None


_otel_probe_cache: dict[str, Any | None] = {}


def otel_tracer_cached() -> Any | None:
    """Cached OpenTelemetry tracer probe; avoids re-importing on every call.

    The cache is keyed by whether tracing is enabled, so it stays correct when
    the env changes. Call :func:`reset_otel_probe_cache` to force a re-probe
    (used by tests).
    """

    key = "on" if tracing_enabled() else "off"
    if key not in _otel_probe_cache:
        _otel_probe_cache[key] = _otel_tracer()
    return _otel_probe_cache[key]


def reset_otel_probe_cache() -> None:
    _otel_probe_cache.clear()


@contextmanager
def start_span(
    name: str, *, traceparent: str | None = None, **attributes: Any
) -> Iterator[NoOpSpan]:
    """Start a span; always yields a span object, never raises."""

    parent = parse_traceparent(traceparent) if traceparent else None
    trace_id = (parent or {}).get("trace_id") or get_field("trace_id") or new_trace_id()
    span_id = new_span_id()
    delegate = None
    tracer = otel_tracer_cached()
    span = NoOpSpan(name, trace_id, span_id, delegate)
    for key, value in attributes.items():
        span.set_attribute(key, value)
    if tracer is not None and export_enabled():  # pragma: no cover - optional path
        try:
            with tracer.start_as_current_span(name) as delegate_span:
                span._delegate = delegate_span
                with correlation_scope(trace_id=trace_id, span_id=span_id):
                    yield span
            return
        except Exception:
            pass
    with correlation_scope(trace_id=trace_id, span_id=span_id):
        yield span


def tracing_status() -> dict[str, Any]:
    return {
        "enabled": tracing_enabled(),
        "export_enabled": export_enabled(),
        "implementation": "opentelemetry" if otel_tracer_cached() is not None else "noop",
        "propagation": "w3c-traceparent",
    }
