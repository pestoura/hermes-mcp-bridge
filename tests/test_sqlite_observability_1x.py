"""Regression gates for bounded 1.x SQLite operation observability."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_mcp_bridge.approvals import ApprovalRegistry
from hermes_mcp_bridge.models import ApprovalRecord, ApprovalStatus
from hermes_mcp_bridge.observability.metrics import get_registry, render_prometheus
from hermes_mcp_bridge.observability.sqlite import (
    instrument_sqlite_registries,
    observe_sqlite,
    sqlite_instrumentation_coverage,
)


def setup_function() -> None:
    get_registry().reset()


def test_sqlite_registry_instrumentation_is_complete_and_idempotent() -> None:
    instrument_sqlite_registries()
    first = sqlite_instrumentation_coverage()
    instrument_sqlite_registries()
    second = sqlite_instrumentation_coverage()

    assert first == {"expected": 21, "covered": 21}
    assert second == first


def test_real_approval_registry_operations_record_latency_and_return_inflight_to_zero(
    tmp_path,
) -> None:
    instrument_sqlite_registries()
    db_path = str(tmp_path / "sensitive-db-name.sqlite3")
    registry = ApprovalRegistry(db_path)
    registry.initialize()

    approval = ApprovalRecord(
        approval_id="approval-id-must-not-escape",
        action="hermes_stop",
        resource="resource-must-not-escape",
        principal="principal-must-not-escape",
        decision=ApprovalStatus.REQUESTED,
    )
    registry.create(approval)
    loaded = registry.get(approval.approval_id)
    assert loaded.approval_id == approval.approval_id

    metrics = get_registry()
    successful = metrics.histogram(
        "bridge_sqlite_operation_duration_seconds", "unused"
    ).snapshot(kind="approvals", outcome="success")
    # create() performs its own write and a real nested get(); the explicit get
    # above is a third operation. Counting all three exposes actual DB chatter.
    assert successful["count"] >= 3
    assert successful["sum"] >= 0.0
    assert metrics.gauge(
        "bridge_sqlite_transactions_inflight", "unused"
    ).value(kind="approvals") == 0.0

    text = render_prometheus()
    assert "sensitive-db-name" not in text
    assert "approval-id-must-not-escape" not in text
    assert "resource-must-not-escape" not in text
    assert "principal-must-not-escape" not in text
    assert "SELECT" not in text
    assert "INSERT" not in text


def test_sqlite_error_timing_preserves_original_exception() -> None:
    @observe_sqlite("state")
    def fail_exactly() -> None:
        raise sqlite3.OperationalError("synthetic locked database marker")

    with pytest.raises(sqlite3.OperationalError, match="synthetic locked database marker"):
        fail_exactly()

    metrics = get_registry()
    error = metrics.histogram(
        "bridge_sqlite_operation_duration_seconds", "unused"
    ).snapshot(kind="state", outcome="error")
    assert error["count"] == 1
    assert metrics.gauge(
        "bridge_sqlite_transactions_inflight", "unused"
    ).value(kind="state") == 0.0

    text = render_prometheus()
    assert "synthetic locked database marker" not in text


def test_unknown_kind_falls_back_to_other_without_new_cardinality() -> None:
    @observe_sqlite("user-controlled-kind-must-not-be-label")
    def succeed() -> int:
        return 7

    assert succeed() == 7
    snapshot = get_registry().histogram(
        "bridge_sqlite_operation_duration_seconds", "unused"
    ).snapshot(kind="other", outcome="success")
    assert snapshot["count"] == 1
    assert "user-controlled-kind-must-not-be-label" not in render_prometheus()


def test_sqlite_observability_has_no_unbounded_labels() -> None:
    instrument_sqlite_registries()
    health = get_registry().health()
    assert health["unbounded_labels"] == []
