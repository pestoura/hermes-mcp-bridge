"""Structured observability for the Hermes MCP bridge.

Submodules:

* :mod:`.redaction` — central fail-closed redaction.
* :mod:`.context` — contextvar-based correlation context.
* :mod:`.logging` — deterministic JSON/text structured logging.
* :mod:`.metrics` — thread-safe, low-cardinality metric registry.
* :mod:`.exporter` — loopback-only Prometheus endpoint.
* :mod:`.tracing` — no-op spans with optional OpenTelemetry and W3C propagation.
* :mod:`.instrumentation` — central wrappers for tools, upstream calls and SSE.
"""

from __future__ import annotations

from typing import Any

from .context import (
    CONTEXT_FIELDS,
    clear_context,
    correlation_scope,
    get_context,
    get_field,
    new_correlation_id,
    set_field,
)
from .exporter import (
    MetricsExporter,
    MetricsExporterError,
    exporter_status,
    metrics_enabled,
    start_exporter_if_enabled,
)
from .instrumentation import (
    endpoint_class,
    instrument_all_tools,
    instrument_tool,
    record_approval,
    record_backoff_sleep,
    record_circuit_rejection,
    record_circuit_transition,
    record_duplicate_event,
    record_out_of_order_event,
    record_polling_iteration,
    record_recovery,
    record_sqlite_error,
    record_sqlite_operation,
    record_sqlite_retry,
    record_sse_connection,
    record_sse_fallback,
    record_upstream,
    set_active_runs,
    set_migrations_version,
    status_class,
)
from .logging import (
    configure_logging,
    get_logger,
    log_event,
    log_mode,
    observability_status,
    timed_event,
)
from .metrics import (
    BOUNDED_LABEL_VALUES,
    CONTENT_TYPE,
    CardinalityError,
    MetricsRegistry,
    get_metrics,
    get_registry,
    render_prometheus,
    set_bridge_info,
)
from .redaction import REDACTED, redact_text, sanitize
from .tracing import (
    NoOpSpan,
    build_trace_metadata,
    format_traceparent,
    parse_traceparent,
    sanitize_trace_context,
    start_span,
    tracing_readiness,
    tracing_status,
)

__all__ = [
    "BOUNDED_LABEL_VALUES",
    "CONTENT_TYPE",
    "CONTEXT_FIELDS",
    "REDACTED",
    "CardinalityError",
    "MetricsExporter",
    "MetricsExporterError",
    "MetricsRegistry",
    "NoOpSpan",
    "build_trace_metadata",
    "clear_context",
    "configure_logging",
    "correlation_scope",
    "endpoint_class",
    "exporter_status",
    "format_traceparent",
    "get_context",
    "get_field",
    "get_logger",
    "get_metrics",
    "get_registry",
    "instrument_all_tools",
    "instrument_tool",
    "log_event",
    "log_mode",
    "metrics_enabled",
    "new_correlation_id",
    "observability_status",
    "parse_traceparent",
    "record_approval",
    "record_backoff_sleep",
    "record_circuit_rejection",
    "record_circuit_transition",
    "record_duplicate_event",
    "record_out_of_order_event",
    "record_polling_iteration",
    "record_recovery",
    "record_sqlite_error",
    "record_sqlite_operation",
    "record_sqlite_retry",
    "record_sse_connection",
    "record_sse_fallback",
    "record_upstream",
    "redact_text",
    "render_prometheus",
    "sanitize",
    "sanitize_trace_context",
    "set_active_runs",
    "set_bridge_info",
    "set_field",
    "set_migrations_version",
    "start_exporter_if_enabled",
    "start_span",
    "status_class",
    "timed_event",
    "tracing_readiness",
    "tracing_status",
]


def _retry_health() -> dict[str, object]:
    """Return retry posture without making health depend on settings parsing."""

    try:
        from ..config import get_settings
        from ..resilience.http_retry import retry_posture

        return {"status": "ready", **retry_posture(get_settings())}
    except Exception:
        return {
            "status": "not_ready",
            "enabled": False,
            "max_attempts": 1,
            "safe_endpoint_classes": [],
            "mutations_retryable": False,
            "sse_retryable": False,
        }


def _circuit_health() -> dict[str, object]:
    """Return circuit posture without exposing settings or endpoint identifiers."""

    try:
        from ..config import get_settings
        from ..resilience.http_circuit import circuit_posture

        return {"status": "ready", **circuit_posture(get_settings())}
    except Exception:
        return {
            "status": "not_ready",
            "enabled": False,
            "safe_endpoint_classes": [],
            "mutations_protected": False,
            "sse_protected": False,
            "breakers": [],
        }


def observability_health() -> dict[str, Any]:
    """Aggregate, secret-free operational status for health/readiness."""

    return {
        "logging": observability_status(),
        "metrics": exporter_status(),
        "metrics_registry": get_registry().health(),
        "tracing": tracing_status(),
        "retry": _retry_health(),
        "circuit_breaker": _circuit_health(),
    }
