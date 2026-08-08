"""Regression gates for 1.x lock/checkpoint/saga/quota observability."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from hermes_mcp_bridge.locks import LockRegistry
from hermes_mcp_bridge.models import LockType, ResourceLock
from hermes_mcp_bridge.observability import state_governance
from hermes_mcp_bridge.observability.metrics import get_registry, render_prometheus


def setup_function() -> None:
    get_registry().reset()


def test_lock_tool_events_are_bounded_and_identifiers_do_not_escape(monkeypatch) -> None:
    monkeypatch.setattr(state_governance, "refresh_locks_active", lambda: None)

    state_governance.observe_state_tool_result(
        "hermes_lock_acquire",
        {
            "lock_key": "secret-lock-key",
            "owner": "sensitive-owner",
            "status": "active",
            "acquired_at": datetime.now(UTC).isoformat(),
        },
    )
    state_governance.observe_state_tool_result(
        "hermes_lock_acquire",
        {"lock_key": "secret-lock-key", "error": "lock conflict on secret-lock-key"},
    )
    state_governance.observe_state_tool_result(
        "hermes_lock_release",
        {
            "lock_key": "secret-lock-key",
            "owner": "sensitive-owner",
            "acquired_at": (datetime.now(UTC) - timedelta(seconds=4)).isoformat(),
        },
    )

    counter = get_registry().counter("bridge_lock_events_total", "unused")
    assert counter.value(outcome="acquired") == 1.0
    assert counter.value(outcome="conflict") == 1.0
    assert counter.value(outcome="released") == 1.0
    duration = get_registry().histogram(
        "bridge_lock_duration_seconds", "unused"
    ).snapshot(outcome="released")
    assert duration["count"] == 1
    assert duration["sum"] >= 3.0

    text = render_prometheus()
    assert "secret-lock-key" not in text
    assert "sensitive-owner" not in text
    assert "lock_key=" not in text
    assert "owner=" not in text


def test_real_lock_expiry_transition_is_counted_once(tmp_path) -> None:
    registry = LockRegistry(str(tmp_path / "state.sqlite3"))
    registry.initialize()
    registry.acquire(
        ResourceLock(
            lock_key="expiring-resource",
            lock_type=LockType.WRITE_EXCLUSIVE,
            owner="owner-a",
            ttl_seconds=1,
        )
    )

    # Move the persisted expiry into the past without invoking the reaper.
    with sqlite3.connect(str(tmp_path / "state.sqlite3")) as cx:
        expires_at = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
        cx.execute(
            "UPDATE resource_locks SET expires_at = ? "
            "WHERE lock_key = ? AND owner = ?",
            (expires_at, "expiring-resource", "owner-a"),
        )

    registry.acquire(
        ResourceLock(
            lock_key="another-resource",
            lock_type=LockType.WRITE_EXCLUSIVE,
            owner="owner-b",
            ttl_seconds=30,
        )
    )
    counter = get_registry().counter("bridge_lock_events_total", "unused")
    assert counter.value(outcome="expired") == 1.0

    # A second acquire cannot double-count the already transitioned row.
    registry.acquire(
        ResourceLock(
            lock_key="third-resource",
            lock_type=LockType.WRITE_EXCLUSIVE,
            owner="owner-c",
            ttl_seconds=30,
        )
    )
    assert counter.value(outcome="expired") == 1.0


def test_checkpoint_and_continuation_events_are_event_only() -> None:
    state_governance.observe_state_tool_result(
        "hermes_checkpoint_create",
        {"checkpoint_id": "checkpoint-secret", "execution_id": "run-secret"},
    )
    state_governance.observe_state_tool_result(
        "hermes_continue",
        {"continuation_id": "continuation-secret", "resume_supported": True},
    )
    state_governance.observe_state_tool_result(
        "hermes_continue",
        {"continuation_id": "continuation-unsupported", "resume_supported": False},
    )

    registry = get_registry()
    assert registry.counter(
        "bridge_checkpoint_events_total", "unused"
    ).value(outcome="created") == 1.0
    continuation = registry.counter("bridge_continuation_events_total", "unused")
    assert continuation.value(outcome="created") == 1.0
    assert continuation.value(outcome="unsupported") == 1.0

    text = render_prometheus()
    assert "checkpoint-secret" not in text
    assert "continuation-secret" not in text
    assert "run-secret" not in text


def test_saga_events_and_compensations_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(state_governance, "_saga_duration_from_result", lambda _result: 2.5)
    state_governance.observe_state_tool_result(
        "hermes_saga_start",
        {"saga_id": "saga-secret", "execution_id": "run-secret", "status": "running"},
    )
    state_governance.observe_state_tool_result(
        "hermes_saga_compensate",
        {"saga_id": "saga-secret", "step_id": "step-secret", "status": "compensated"},
    )

    registry = get_registry()
    events = registry.counter("bridge_saga_events_total", "unused")
    assert events.value(outcome="started") == 1.0
    assert events.value(outcome="compensated") == 1.0
    assert registry.counter(
        "bridge_saga_compensations_total", "unused"
    ).value(outcome="compensated") == 1.0
    duration = registry.histogram(
        "bridge_saga_duration_seconds", "unused"
    ).snapshot(outcome="compensated")
    assert duration == {"count": 1, "sum": 2.5}

    text = render_prometheus()
    assert "saga-secret" not in text
    assert "step-secret" not in text
    assert "run-secret" not in text


def test_quota_status_is_explicitly_not_enforced() -> None:
    result = state_governance.observe_state_tool_result(
        "hermes_quota_status",
        {
            "quota": {"decision": "ALLOW", "profile_id": "default"},
            "status": {"profiles": []},
        },
    )
    assert result["enforcement_status"] == "NOT_ENFORCED"
    assert result["accounting"] == {
        "parallel_runs": False,
        "runtime": False,
        "tool_calls": False,
        "mutation_concurrency": False,
        "tokens": False,
    }
    assert get_registry().gauge(
        "bridge_quota_enforcement_active", "unused"
    ).value() == 0.0


def test_state_governance_cardinality_remains_bounded() -> None:
    health = get_registry().health()
    assert health["unbounded_labels"] == []
