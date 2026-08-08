"""Low-cardinality governance observability for the 1.x bridge.

This module contains fail-open metric helpers only. It never emits principals,
resources, signatures, secrets, approval identifiers, prompts or payload
contents. Approval identifiers may be used transiently for an internal registry
lookup but are never labels or telemetry fields. Domains are finite by design.
"""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from .metrics import BOUNDED_LABEL_VALUES, get_registry

_POLICY_DECISIONS = frozenset({"allow", "deny", "require_approval", "other"})
_HMAC_OUTCOMES = frozenset(
    {"valid_current", "valid_previous", "invalid", "missing", "config_error", "other"}
)
_APPROVAL_OUTCOMES = frozenset({"approved", "rejected", "expired", "other"})
_APPROVAL_TOOLS = frozenset(
    {"hermes_approval_create", "hermes_approval_respond", "hermes_approval_status"}
)

# Extend the existing bounded outcome domain before any governance metric is
# created. Cardinality validation therefore remains centralized and fail-closed.
BOUNDED_LABEL_VALUES["outcome"] = frozenset(
    BOUNDED_LABEL_VALUES["outcome"] | _HMAC_OUTCOMES | _APPROVAL_OUTCOMES
)


def _counter(name: str, help_text: str) -> Any:
    return get_registry().counter(name, help_text)


def _histogram(name: str, help_text: str) -> Any:
    return get_registry().histogram(name, help_text)


def _gauge(name: str, help_text: str) -> Any:
    return get_registry().gauge(name, help_text)


def _normalize(value: object, allowed: frozenset[str]) -> str:
    normalized = str(getattr(value, "value", value) or "other").strip().lower()
    normalized = normalized.replace("-", "_")
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


def record_policy_evaluation(decision: object, duration_seconds: float) -> None:
    """Record one real policy decision and its local evaluation latency."""

    try:
        normalized = _normalize(decision, _POLICY_DECISIONS)
        _counter(
            "bridge_policy_decisions_total",
            "Policy evaluations by bounded decision.",
        ).inc(decision=normalized)
        _histogram(
            "bridge_policy_evaluation_duration_seconds",
            "Local policy evaluation duration in seconds by bounded decision.",
        ).observe(max(0.0, float(duration_seconds)), decision=normalized)
    except Exception:
        return


def record_hmac_validation(outcome: str) -> None:
    """Record only the bounded result class of a verification attempt."""

    try:
        normalized = _normalize(outcome, _HMAC_OUTCOMES)
        _counter(
            "bridge_hmac_validations_total",
            "HMAC validation attempts by bounded outcome.",
        ).inc(outcome=normalized)
    except Exception:
        return


def record_approval_wait(outcome: str, duration_seconds: float) -> None:
    """Record actual human approval wait after a terminal decision."""

    try:
        normalized = _normalize(outcome, _APPROVAL_OUTCOMES)
        _histogram(
            "bridge_approval_wait_seconds",
            "Elapsed approval wait in seconds by bounded terminal outcome.",
        ).observe(max(0.0, float(duration_seconds)), outcome=normalized)
    except Exception:
        return


def set_approvals_pending(count: int) -> None:
    """Publish the current number of approvals awaiting a human decision."""

    with suppress(Exception):
        _gauge(
            "bridge_approvals_pending",
            "Approvals currently awaiting a human decision.",
        ).set(float(max(0, int(count))))


def refresh_approvals_pending() -> None:
    """Read the registry exactly and publish non-expired REQUESTED approvals.

    The query is explicitly read-only (``PRAGMA query_only=ON``) and does not
    invoke migrations, expiry transitions or any other governance mutation.
    """

    try:
        from ..approvals import get_approval_registry

        registry = get_approval_registry()
        db_path = str(registry._db_path)  # package-private, read-only introspection
        connection = sqlite3.connect(db_path, check_same_thread=False)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=1000")
            rows = connection.execute(
                "SELECT expires_at FROM approvals WHERE decision = ?",
                ("requested",),
            ).fetchall()
        finally:
            connection.close()

        now = datetime.now(UTC)
        pending = 0
        for (expires_at,) in rows:
            if expires_at is None:
                pending += 1
                continue
            expiry = _parse_time(expires_at)
            if expiry is not None and expiry > now:
                pending += 1
        set_approvals_pending(pending)
    except Exception:
        return


def observe_approval_tool_result(tool_name: str, result: Any) -> None:
    """Observe actual approval lifecycle results without exposing identifiers."""

    if tool_name not in _APPROVAL_TOOLS or not isinstance(result, dict):
        return
    try:
        # Refresh even for an error response: this keeps the gauge useful after
        # failed/expired responses without changing the registry state.
        refresh_approvals_pending()
        if tool_name != "hermes_approval_respond" or result.get("error"):
            return

        approval_id = str(result.get("approval_id") or "").strip()
        if not approval_id:
            return

        from ..approvals import get_approval_registry

        record = get_approval_registry().get(approval_id)
        outcome = str(getattr(record.decision, "value", record.decision) or "other").lower()
        if outcome not in {"approved", "rejected"}:
            return
        created = _parse_time(record.created_at)
        decided = _parse_time(record.decided_at)
        if created is None or decided is None or decided < created:
            return
        record_approval_wait(outcome, (decided - created).total_seconds())
    except Exception:
        return
