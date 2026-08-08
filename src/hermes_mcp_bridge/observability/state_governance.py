"""Low-cardinality observability for 1.x state/governance primitives.

The helpers in this module observe existing lock, checkpoint, continuation, saga
and quota behavior. Identifiers are used only for internal read-only lookups and
are never exported as labels or log fields. Telemetry is fail-open.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from .metrics import BOUNDED_LABEL_VALUES, get_registry

_LOCK_OUTCOMES = frozenset({"acquired", "released", "conflict", "expired", "error", "other"})
_CHECKPOINT_OUTCOMES = frozenset({"created", "error", "other"})
_CONTINUATION_OUTCOMES = frozenset({"created", "unsupported", "error", "other"})
_SAGA_OUTCOMES = frozenset(
    {"started", "running", "completed", "compensating", "compensated", "failed", "error", "other"}
)

BOUNDED_LABEL_VALUES["outcome"] = frozenset(
    BOUNDED_LABEL_VALUES["outcome"]
    | _LOCK_OUTCOMES
    | _CHECKPOINT_OUTCOMES
    | _CONTINUATION_OUTCOMES
    | _SAGA_OUTCOMES
)


def _counter(name: str, help_text: str) -> Any:
    return get_registry().counter(name, help_text)


def _gauge(name: str, help_text: str) -> Any:
    return get_registry().gauge(name, help_text)


def _histogram(name: str, help_text: str) -> Any:
    return get_registry().histogram(name, help_text)


def _normalize(value: object, allowed: frozenset[str]) -> str:
    normalized = str(getattr(value, "value", value) or "other").strip().lower().replace("-", "_")
    return normalized if normalized in allowed else "other"


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def record_lock_event(outcome: str, *, duration_seconds: float | None = None, count: int = 1) -> None:
    """Record a lock lifecycle event with a finite outcome domain."""

    try:
        normalized = _normalize(outcome, _LOCK_OUTCOMES)
        _counter(
            "bridge_lock_events_total",
            "Resource lock lifecycle events by bounded outcome.",
        ).inc(float(max(0, int(count))), outcome=normalized)
        if duration_seconds is not None:
            _histogram(
                "bridge_lock_duration_seconds",
                "Observed lock lifetime in seconds by bounded terminal outcome.",
            ).observe(max(0.0, float(duration_seconds)), outcome=normalized)
    except Exception:
        return


def refresh_locks_active() -> None:
    """Publish live ACTIVE locks without mutating expiry state."""

    try:
        from ..server import lock_registry

        now = datetime.now(UTC)
        active = 0
        for item in lock_registry.list_status():
            if str(item.get("status") or "").lower() != "active":
                continue
            expires = _parse_time(item.get("expires_at"))
            if expires is not None and expires <= now:
                continue
            active += 1
        _gauge(
            "bridge_locks_active",
            "Resource locks currently active and not past their expiry time.",
        ).set(float(active))
    except Exception:
        return


def record_checkpoint_event(outcome: str) -> None:
    with suppress(Exception):
        _counter(
            "bridge_checkpoint_events_total",
            "Checkpoint persistence events by bounded outcome.",
        ).inc(outcome=_normalize(outcome, _CHECKPOINT_OUTCOMES))


def record_continuation_event(outcome: str) -> None:
    with suppress(Exception):
        _counter(
            "bridge_continuation_events_total",
            "Continuation persistence events by bounded outcome.",
        ).inc(outcome=_normalize(outcome, _CONTINUATION_OUTCOMES))


def record_saga_event(outcome: str, *, duration_seconds: float | None = None) -> None:
    try:
        normalized = _normalize(outcome, _SAGA_OUTCOMES)
        _counter(
            "bridge_saga_events_total",
            "Saga lifecycle events by bounded outcome.",
        ).inc(outcome=normalized)
        if duration_seconds is not None:
            _histogram(
                "bridge_saga_duration_seconds",
                "Observed saga lifetime in seconds by bounded terminal outcome.",
            ).observe(max(0.0, float(duration_seconds)), outcome=normalized)
    except Exception:
        return


def record_saga_compensation(outcome: str) -> None:
    with suppress(Exception):
        _counter(
            "bridge_saga_compensations_total",
            "Saga compensation attempts by bounded outcome.",
        ).inc(outcome=_normalize(outcome, _SAGA_OUTCOMES))


def set_quota_not_enforced() -> None:
    """State the only currently supportable accounting conclusion."""

    with suppress(Exception):
        _gauge(
            "bridge_quota_enforcement_active",
            "Whether real quota accounting/enforcement is active (1=yes, 0=no).",
        ).set(0.0)


def _saga_duration_from_result(result: dict[str, Any]) -> float | None:
    saga_id = str(result.get("saga_id") or "").strip()
    if not saga_id:
        return None
    try:
        from ..server import saga_registry

        saga = saga_registry.get(saga_id)
        if saga is None:
            return None
        created = _parse_time(saga.created_at)
        updated = _parse_time(saga.updated_at)
        if created is None or updated is None or updated < created:
            return None
        return (updated - created).total_seconds()
    except Exception:
        return None


def observe_state_tool_result(tool_name: str, result: Any) -> Any:
    """Observe one MCP state primitive and optionally add truthful quota status."""

    if not isinstance(result, dict):
        return result
    error = bool(result.get("error"))

    if tool_name == "hermes_lock_acquire":
        if error:
            message = str(result.get("error") or "").lower()
            record_lock_event("conflict" if "conflict" in message else "error")
        else:
            record_lock_event("acquired")
        refresh_locks_active()
    elif tool_name == "hermes_lock_release":
        if error:
            record_lock_event("error")
        else:
            acquired = _parse_time(result.get("acquired_at"))
            duration = None
            if acquired is not None:
                duration = max(0.0, (datetime.now(UTC) - acquired).total_seconds())
            record_lock_event("released", duration_seconds=duration)
        refresh_locks_active()
    elif tool_name == "hermes_lock_status":
        refresh_locks_active()
    elif tool_name == "hermes_checkpoint_create":
        record_checkpoint_event("error" if error else "created")
    elif tool_name == "hermes_continue":
        if error:
            record_continuation_event("error")
        elif result.get("resume_supported") is False:
            record_continuation_event("unsupported")
        else:
            record_continuation_event("created")
    elif tool_name == "hermes_saga_start":
        record_saga_event("error" if error else "started")
    elif tool_name == "hermes_saga_compensate":
        if error:
            record_saga_compensation("error")
            record_saga_event("error")
        else:
            outcome = _normalize(result.get("status"), _SAGA_OUTCOMES)
            record_saga_compensation(outcome)
            duration = _saga_duration_from_result(result) if outcome in {"completed", "compensated", "failed"} else None
            record_saga_event(outcome, duration_seconds=duration)
    elif tool_name == "hermes_quota_status":
        set_quota_not_enforced()
        augmented = dict(result)
        augmented["enforcement_status"] = "NOT_ENFORCED"
        augmented["accounting"] = {
            "parallel_runs": False,
            "runtime": False,
            "tool_calls": False,
            "mutation_concurrency": False,
            "tokens": False,
        }
        return augmented
    return result
