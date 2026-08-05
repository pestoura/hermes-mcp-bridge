"""Deprecated shim for :mod:`hermes_mcp_bridge.observability.tracing`.

The tracing implementation lives in the observability package. This module is
kept as a thin re-export so existing imports
(``from hermes_mcp_bridge.tracing import build_trace_metadata``) keep working;
it will be removed in a future major release.

Behaviour note: ``parse_traceparent`` here is now the canonical implementation,
which is *stricter* than the historical root one — it additionally rejects the
forbidden version ``ff``, an all-zero trace id and an all-zero span id, as
required by the W3C trace-context spec. That is a fail-closed tightening, not a
contract break: previously-valid real traceparents still parse identically.

Importing this module emits a :class:`DeprecationWarning`; the bridge routes
warnings into the structured log pipeline, so it never lands as raw stderr text.
"""

from __future__ import annotations

import warnings

from .observability.tracing import (
    TRACE_ALLOWED_FIELDS,
    TRACE_FORBIDDEN_FIELDS,
    build_trace_metadata,
    format_traceparent,
    parse_traceparent,
    sanitize_trace_context,
    tracing_readiness,
    tracing_status,
)

#: Backwards-compatible private aliases (some callers/tests referenced these).
_TRACE_ALLOWED_FIELDS = set(TRACE_ALLOWED_FIELDS)

warnings.warn(
    "hermes_mcp_bridge.tracing is deprecated; import from "
    "hermes_mcp_bridge.observability.tracing instead",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "TRACE_ALLOWED_FIELDS",
    "TRACE_FORBIDDEN_FIELDS",
    "build_trace_metadata",
    "format_traceparent",
    "parse_traceparent",
    "sanitize_trace_context",
    "tracing_readiness",
    "tracing_status",
]
