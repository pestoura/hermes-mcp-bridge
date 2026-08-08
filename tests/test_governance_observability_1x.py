"""Regression gates for bounded 1.x governance observability."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from hermes_mcp_bridge import approvals, signing
from hermes_mcp_bridge.approvals import ApprovalRegistry
from hermes_mcp_bridge.observability.governance import (
    observe_approval_tool_result,
    refresh_approvals_pending,
)
from hermes_mcp_bridge.observability.metrics import get_registry, render_prometheus
from hermes_mcp_bridge.policy import evaluate_policy
from hermes_mcp_bridge.protocol import (
    ApprovalRecord,
    ApprovalStatus,
    MutationClass,
    PolicyEvaluationInput,
    TrustLabel,
)


def setup_function() -> None:
    get_registry().reset()


def _evaluation(action: str) -> PolicyEvaluationInput:
    return PolicyEvaluationInput(
        action=action,
        resource="sensitive-resource-must-not-be-label",
        principal="person-must-not-be-label",
        trust_label=TrustLabel.TRUSTED_POLICY,
        mutation_class=MutationClass.NONE,
    )


def test_policy_metrics_cover_real_bounded_decisions_without_context_labels() -> None:
    assert evaluate_policy(_evaluation("hermes_health")).decision.value == "ALLOW"
    assert evaluate_policy(_evaluation("hermes_stop")).decision.value == "REQUIRE_APPROVAL"
    assert evaluate_policy(_evaluation("unknown-action")).decision.value == "DENY"

    registry = get_registry()
    counter = registry.counter("bridge_policy_decisions_total", "unused")
    assert counter.value(decision="allow") == 1.0
    assert counter.value(decision="require_approval") == 1.0
    assert counter.value(decision="deny") == 1.0

    text = render_prometheus()
    assert "bridge_policy_evaluation_duration_seconds_count" in text
    assert "person-must-not-be-label" not in text
    assert "sensitive-resource-must-not-be-label" not in text
    assert "principal=" not in text
    assert "resource=" not in text


def _digest(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def test_hmac_metrics_report_only_bounded_validation_outcomes() -> None:
    payload = "payload-content-must-not-escape"
    current = "c" * 48
    previous = "p" * 48
    env = {
        "BRIDGE_SECURITY_MODE": "development",
        "HERMES_BRIDGE_HMAC_SECRET": current,
        "HERMES_BRIDGE_HMAC_SECRET_PREVIOUS": previous,
        "HERMES_BRIDGE_MIN_SECRET_LENGTH": "32",
    }

    assert signing.verify(payload, _digest(current, payload), env) is True
    assert signing.verify(payload, _digest(previous, payload), env) is True
    assert signing.verify(payload, "f" * 64, env) is False
    assert signing.verify(payload, "", env) is False
    assert signing.verify(payload, "f" * 64, {"BRIDGE_SECURITY_MODE": "production"}) is False

    counter = get_registry().counter("bridge_hmac_validations_total", "unused")
    assert counter.value(outcome="valid_current") == 1.0
    assert counter.value(outcome="valid_previous") == 1.0
    assert counter.value(outcome="invalid") == 1.0
    assert counter.value(outcome="missing") == 1.0
    assert counter.value(outcome="config_error") == 1.0

    text = render_prometheus()
    assert current not in text
    assert previous not in text
    assert payload not in text
    assert "signature=" not in text
    assert "key_id=" not in text


def test_approval_pending_and_wait_are_derived_from_real_registry_state(tmp_path) -> None:
    original = approvals._approval_registry
    registry = ApprovalRegistry(str(tmp_path / "state.sqlite3"))
    registry.initialize()
    approvals._approval_registry = registry
    try:
        created_at = datetime.now(UTC) - timedelta(seconds=12)
        record = ApprovalRecord(
            approval_id="approval-observability-test",
            action="hermes_stop",
            resource="resource-must-not-escape",
            principal="principal-must-not-escape",
            decision=ApprovalStatus.REQUESTED,
            created_at=created_at.isoformat(),
            expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        )
        registry.create(record)

        observe_approval_tool_result(
            "hermes_approval_create",
            {"approval_id": record.approval_id, "decision": "requested"},
        )
        assert get_registry().gauge("bridge_approvals_pending", "unused").value() == 1.0

        updated = registry.respond(record.approval_id, ApprovalStatus.APPROVED)
        observe_approval_tool_result(
            "hermes_approval_respond",
            {
                "approval_id": updated.approval_id,
                "decision": updated.decision.value,
                "decided_at": updated.decided_at,
            },
        )

        assert get_registry().gauge("bridge_approvals_pending", "unused").value() == 0.0
        wait = get_registry().histogram(
            "bridge_approval_wait_seconds", "unused"
        ).snapshot(outcome="approved")
        assert wait["count"] == 1
        assert wait["sum"] >= 10.0

        text = render_prometheus()
        assert record.approval_id not in text
        assert "resource-must-not-escape" not in text
        assert "principal-must-not-escape" not in text
        assert "approval_id=" not in text
    finally:
        approvals._approval_registry = original


def test_expired_requested_rows_are_not_reported_pending(tmp_path) -> None:
    original = approvals._approval_registry
    registry = ApprovalRegistry(str(tmp_path / "state.sqlite3"))
    registry.initialize()
    approvals._approval_registry = registry
    try:
        record = ApprovalRecord(
            approval_id="approval-expired-observability-test",
            action="hermes_stop",
            decision=ApprovalStatus.REQUESTED,
            created_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
        registry.create(record)
        refresh_approvals_pending()
        assert get_registry().gauge("bridge_approvals_pending", "unused").value() == 0.0
    finally:
        approvals._approval_registry = original


def test_governance_metrics_keep_cardinality_bounded() -> None:
    health = get_registry().health()
    assert health["unbounded_labels"] == []
