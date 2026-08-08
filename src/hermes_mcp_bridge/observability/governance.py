"""Low-cardinality governance observability for the 1.x bridge.

This module contains fail-open metric helpers only. It never receives or emits
principals, resources, signatures, secrets, approval identifiers, prompts or
payload contents. Domains are finite by construction.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from .metrics import BOUNDED_LABEL_VALUES, get_registry

_POLICY_DECISIONS = frozenset({"allow", "deny", "require_approval", "other"})
_HMAC_OUTCOMES = frozenset(
    {"valid_current", "valid_previous", "invalid", "missing", "config_error", "other"}
)
_APPROVAL_OUTCOMES = frozenset({"approved", "rejected", "expired", "other"})

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
