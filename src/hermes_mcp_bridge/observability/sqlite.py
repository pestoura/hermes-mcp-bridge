"""Fail-open timing for real SQLite registry operations in the 1.x bridge.

The decorator is intentionally small: it measures one existing registry method,
uses only the caller-supplied bounded ``kind`` label, and never inspects SQL,
parameters, paths, identifiers or row contents. Telemetry failure cannot change
the wrapped operation result.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TypeVar, cast

from .metrics import BOUNDED_LABEL_VALUES, get_registry

_F = TypeVar("_F", bound=Callable[..., Any])
_ALLOWED_KINDS = frozenset({"state", "approvals", "locks", "migrations", "recovery", "other"})


def _kind(value: str) -> str:
    normalized = str(value or "other").strip().lower()
    if normalized not in _ALLOWED_KINDS:
        return "other"
    # Keep the central cardinality contract authoritative.
    if normalized not in BOUNDED_LABEL_VALUES.get("kind", frozenset()):
        return "other"
    return normalized


def _inflight(kind: str, delta: float) -> None:
    with suppress(Exception):
        gauge = get_registry().gauge(
            "bridge_sqlite_transactions_inflight",
            "SQLite registry operations currently executing by bounded kind.",
        )
        if delta > 0:
            gauge.inc(delta, kind=kind)
        else:
            gauge.dec(abs(delta), kind=kind)


def _duration(kind: str, outcome: str, seconds: float) -> None:
    with suppress(Exception):
        get_registry().histogram(
            "bridge_sqlite_operation_duration_seconds",
            "SQLite registry operation duration in seconds by kind and outcome.",
        ).observe(max(0.0, float(seconds)), kind=kind, outcome=outcome)


def observe_sqlite(kind: str) -> Callable[[_F], _F]:
    """Measure one synchronous SQLite-backed registry operation.

    ``kind`` is normalized against the existing bounded label contract. The
    wrapper deliberately does not catch the application exception: it records
    ``error`` and re-raises the exact exception unchanged.
    """

    resolved = _kind(kind)

    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            outcome = "success"
            _inflight(resolved, 1.0)
            try:
                return func(*args, **kwargs)
            except Exception:
                outcome = "error"
                raise
            finally:
                _duration(resolved, outcome, time.perf_counter() - started)
                _inflight(resolved, -1.0)

        return cast(_F, wrapper)

    return decorator
