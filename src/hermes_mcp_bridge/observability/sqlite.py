"""Fail-open timing for real SQLite registry operations in the 1.x bridge.

The instrumentation measures existing registry methods, uses only bounded
``kind`` labels and never inspects SQL, parameters, paths, identifiers or row
contents. Telemetry failure cannot change the wrapped operation result.
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

# These are public registry methods that execute SQLite work in the current 1.x
# implementation. Nested calls are intentionally counted because they are real
# extra DB round-trips (for example acquire() followed by get()).
_REGISTRY_METHODS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "hermes_mcp_bridge.approvals",
        "ApprovalRegistry",
        "approvals",
        ("health", "create", "get", "respond", "consume", "mark_stale", "expire", "list_recent"),
    ),
    (
        "hermes_mcp_bridge.locks",
        "LockRegistry",
        "locks",
        ("acquire", "release", "get", "list_status"),
    ),
    (
        "hermes_mcp_bridge.checkpoints",
        "CheckpointRegistry",
        "state",
        ("create", "status", "add_continuation", "list_continuations"),
    ),
    (
        "hermes_mcp_bridge.sagas",
        "SagaRegistry",
        "state",
        ("create", "get", "update_status"),
    ),
    (
        "hermes_mcp_bridge.quotas",
        "QuotaRegistry",
        "state",
        ("ensure_default_profile", "status"),
    ),
)


def _kind(value: str) -> str:
    normalized = str(value or "other").strip().lower()
    if normalized not in _ALLOWED_KINDS:
        return "other"
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
    """Measure one synchronous SQLite-backed registry operation."""

    resolved = _kind(kind)

    def decorator(func: _F) -> _F:
        if getattr(func, "__bridge_sqlite_observed__", False):
            return func

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

        wrapper.__bridge_sqlite_observed__ = True  # type: ignore[attr-defined]
        wrapper.__bridge_sqlite_kind__ = resolved  # type: ignore[attr-defined]
        return cast(_F, wrapper)

    return decorator


def instrument_sqlite_registries() -> int:
    """Wrap known SQLite registry methods once and return the wrapped count.

    Structural/import changes fail open and are visible through the returned
    count/tests. Existing application exceptions remain untouched.
    """

    import importlib

    wrapped = 0
    for module_name, class_name, kind, methods in _REGISTRY_METHODS:
        try:
            module = importlib.import_module(module_name)
            registry_class = getattr(module, class_name)
        except Exception:
            continue
        for method_name in methods:
            try:
                original = getattr(registry_class, method_name)
                if getattr(original, "__bridge_sqlite_observed__", False):
                    continue
                setattr(registry_class, method_name, observe_sqlite(kind)(original))
                wrapped += 1
            except Exception:
                continue
    return wrapped


def sqlite_instrumentation_coverage() -> dict[str, int]:
    """Return non-sensitive expected/covered method counts for CI/readiness."""

    import importlib

    expected = sum(len(item[3]) for item in _REGISTRY_METHODS)
    covered = 0
    for module_name, class_name, _kind_name, methods in _REGISTRY_METHODS:
        try:
            module = importlib.import_module(module_name)
            registry_class = getattr(module, class_name)
        except Exception:
            continue
        for method_name in methods:
            with suppress(Exception):
                method = getattr(registry_class, method_name)
                if getattr(method, "__bridge_sqlite_observed__", False):
                    covered += 1
    return {"expected": expected, "covered": covered}
