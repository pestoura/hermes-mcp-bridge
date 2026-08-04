"""Tracing helpers for W3C trace context propagation and sanitized bridge metadata."""

from __future__ import annotations

import re
from typing import Any

_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})"
    r"-(?P<trace_id>[0-9a-f]{32})"
    r"-(?P<span_id>[0-9a-f]{16})"
    r"-(?P<trace_flags>[0-9a-f]{2})$"
)

_TRACE_ALLOWED_FIELDS = {
    "traceparent",
    "tracestate",
    "correlation_id",
    "causation_id",
}


def _is_allowed_context_key(key: str) -> bool:
    return key in _TRACE_ALLOWED_FIELDS


def sanitize_trace_context(context: dict[str, Any] | None) -> dict[str, Any]:
    context = {str(k): str(v) for k, v in (context or {}).items() if k and v is not None}
    return {k: v for k, v in context.items() if _is_allowed_context_key(k)}


def parse_traceparent(traceparent: str) -> dict[str, str] | None:
    if not isinstance(traceparent, str):
        return None
    match = _TRACEPARENT_RE.fullmatch(traceparent.strip())
    if not match:
        return None
    return {
        "version": match.group("version"),
        "trace_id": match.group("trace_id"),
        "span_id": match.group("span_id"),
        "trace_flags": match.group("trace_flags"),
    }


def build_trace_metadata(
    trace_context: dict[str, Any] | None,
    upstream_supported: bool,
) -> dict[str, Any]:
    sanitized = sanitize_trace_context(trace_context)
    metadata: dict[str, Any] = {
        "effective_support": "native" if upstream_supported else "bridge_only",
        "upstream_supported": upstream_supported,
        "advisory": not upstream_supported,
    }
    traceparent = sanitized.get("traceparent")
    if traceparent:
        parsed = parse_traceparent(traceparent)
        if parsed:
            metadata["trace_id"] = parsed["trace_id"]
            metadata["span_id"] = parsed["span_id"]
            metadata["parent"] = parsed["trace_flags"]
    if "correlation_id" in sanitized:
        metadata["correlation"] = sanitized["correlation_id"]
    if "causation_id" in sanitized:
        metadata["causation"] = sanitized["causation_id"]
    for forbidden in {"prompt", "output", "tokens", "leaseToken", "lease_token", "secret"}:
        sanitized.pop(forbidden, None)
    metadata["context"] = sanitized
    return metadata


def tracing_readiness() -> dict[str, Any]:
    return {
        "tracing_ready": True,
        "sanitization_ready": True,
        "bridge_only_default": True,
        "allowed_context_fields": sorted(_TRACE_ALLOWED_FIELDS),
    }
