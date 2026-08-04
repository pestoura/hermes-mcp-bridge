"""Correlation context using contextvars.

Context is task-local: asyncio tasks created inside a bound scope inherit a
*copy*, so sibling tasks never observe each other's values, and the scope is
always restored on exit (including on exception or cancellation).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

_CORRELATION_ID: ContextVar[str | None] = ContextVar("bridge_correlation_id", default=None)
_TRACE_ID: ContextVar[str | None] = ContextVar("bridge_trace_id", default=None)
_SPAN_ID: ContextVar[str | None] = ContextVar("bridge_span_id", default=None)
_EXECUTION_ID: ContextVar[str | None] = ContextVar("bridge_execution_id", default=None)
_RUN_ID: ContextVar[str | None] = ContextVar("bridge_run_id", default=None)
_SESSION_ID: ContextVar[str | None] = ContextVar("bridge_session_id", default=None)
_TOOL_NAME: ContextVar[str | None] = ContextVar("bridge_tool_name", default=None)

_VARS: dict[str, ContextVar[str | None]] = {
    "correlation_id": _CORRELATION_ID,
    "trace_id": _TRACE_ID,
    "span_id": _SPAN_ID,
    "execution_id": _EXECUTION_ID,
    "run_id": _RUN_ID,
    "session_id": _SESSION_ID,
    "tool_name": _TOOL_NAME,
}

CONTEXT_FIELDS: tuple[str, ...] = tuple(_VARS)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def get_context() -> dict[str, str]:
    """Return the current non-empty correlation fields."""

    return {name: var.get() for name, var in _VARS.items() if var.get()}  # type: ignore[misc]


def set_field(name: str, value: str | None) -> None:
    var = _VARS.get(name)
    if var is None:
        return
    var.set(str(value) if value is not None else None)


def get_field(name: str) -> str | None:
    var = _VARS.get(name)
    return var.get() if var is not None else None


@contextmanager
def correlation_scope(**fields: Any) -> Iterator[dict[str, str]]:
    """Bind correlation fields for the duration of the block.

    Unknown keys are ignored. ``correlation_id`` is generated when absent so
    every scope is always correlatable.
    """

    tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = []
    values = {k: v for k, v in fields.items() if k in _VARS}
    if not values.get("correlation_id") and not _CORRELATION_ID.get():
        values["correlation_id"] = new_correlation_id()
    try:
        for name, value in values.items():
            var = _VARS[name]
            tokens.append((var, var.set(str(value) if value is not None else None)))
        yield get_context()
    finally:
        for var, token in reversed(tokens):
            try:
                var.reset(token)
            except ValueError:  # pragma: no cover - token from a different context
                var.set(None)


def clear_context() -> None:
    """Reset every correlation field (test/utility helper)."""

    for var in _VARS.values():
        var.set(None)
