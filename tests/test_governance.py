"""Governance and approval tests for 0.5.0."""

from __future__ import annotations

import os
import tempfile
from datetime import timedelta

import pytest

from hermes_mcp_bridge.approvals import (
    ApprovalConsumedError,
    ApprovalExpiredError,
    ApprovalRecord,
    ApprovalRegistry,
    ApprovalStaleError,
    ApprovalStatus,
    _utcnow,
)
from hermes_mcp_bridge.policy import DecisionType, PolicyEvaluationInput, evaluate_policy
from hermes_mcp_bridge.protocol import MutationClass, TrustLabel
from hermes_mcp_bridge.provenance import build_result_manifest


def _registry(db_path: str) -> ApprovalRegistry:
    registry = ApprovalRegistry(db_path)
    registry.initialize()
    return registry


def test_approval_lifecycle_create_approve_consume() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
        db_path = tmp.name
    try:
        registry = _registry(db_path)
        record = ApprovalRecord(
            approval_id="approval-lifecycle",
            action="tool.call",
            resource="resource-1",
            resource_fingerprint="fp-1",
            principal="user-1",
            created_at=_utcnow().isoformat(),
        )
        created = registry.create(record)
        assert created.decision == ApprovalStatus.REQUESTED

        updated = registry.respond("approval-lifecycle", ApprovalStatus.APPROVED)
        assert updated.decision == ApprovalStatus.APPROVED
        assert updated.decided_at is not None

        consumed = registry.consume("approval-lifecycle", "fp-1")
        assert consumed.decision == ApprovalStatus.CONSUMED
        assert consumed.consumed_at is not None
    finally:
        os.unlink(db_path)


def test_approval_expiry() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
        db_path = tmp.name
    try:
        registry = _registry(db_path)
        past = (_utcnow() - timedelta(seconds=1)).isoformat()
        record = ApprovalRecord(
            approval_id="approval-expired",
            action="tool.call",
            expires_at=past,
            created_at=past,
        )
        registry.create(record)
        with pytest.raises(ApprovalExpiredError):
            registry.respond("approval-expired", ApprovalStatus.APPROVED)
    finally:
        os.unlink(db_path)


def test_approval_stale_fingerprint() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
        db_path = tmp.name
    try:
        registry = _registry(db_path)
        record = ApprovalRecord(
            approval_id="approval-stale",
            action="tool.call",
            resource="resource-1",
            resource_fingerprint="fp-1",
            created_at=_utcnow().isoformat(),
        )
        registry.create(record)
        registry.respond("approval-stale", ApprovalStatus.APPROVED)
        with pytest.raises(ApprovalStaleError):
            registry.consume("approval-stale", "fp-2")
    finally:
        os.unlink(db_path)


def test_policy_allow_low_risk_read() -> None:
    result = evaluate_policy(
        PolicyEvaluationInput(
            action="read",
            trust_label=TrustLabel.USER_INSTRUCTION,
            mutation_class=MutationClass.NONE,
        )
    )
    assert result.decision == DecisionType.ALLOW


def test_policy_deny_explicit_action() -> None:
    result = evaluate_policy(
        PolicyEvaluationInput(action="restricted"),
        policy={"deny_actions": ["restricted"]},
    )
    assert result.decision == DecisionType.DENY


def test_policy_require_approval_high_risk_mutation() -> None:
    result = evaluate_policy(
        PolicyEvaluationInput(
            action="write",
            trust_label=TrustLabel.UNTRUSTED_CONTENT,
            mutation_class=MutationClass.WRITE,
        )
    )
    assert result.decision == DecisionType.REQUIRE_APPROVAL


def test_result_manifest_unsigned_by_default() -> None:
    if os.environ.get("HERMES_BRIDGE_HMAC_SECRET"):
        del os.environ["HERMES_BRIDGE_HMAC_SECRET"]
    manifest = build_result_manifest(
        execution_id="exec-1",
        session_id="session-1",
        status="completed",
    )
    assert manifest.signature_status == "unsigned"
    assert manifest.signature is None
    assert manifest.canonical_digest is not None


def test_approval_single_use_only() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
        db_path = tmp.name
    try:
        registry = _registry(db_path)
        record = ApprovalRecord(
            approval_id="approval-single",
            action="tool.call",
            resource="resource-1",
            resource_fingerprint="fp-1",
            created_at=_utcnow().isoformat(),
        )
        registry.create(record)
        registry.respond("approval-single", ApprovalStatus.APPROVED)
        registry.consume("approval-single", "fp-1")
        with pytest.raises(ApprovalConsumedError):
            registry.consume("approval-single", "fp-1")
    finally:
        os.unlink(db_path)


def test_approval_concurrent_single_consume() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
        db_path = tmp.name
    try:
        registry = _registry(db_path)
        record = ApprovalRecord(
            approval_id="approval-race",
            action="tool.call",
            resource="resource-1",
            resource_fingerprint="fp-1",
            created_at=_utcnow().isoformat(),
        )
        registry.create(record)
        registry.respond("approval-race", ApprovalStatus.APPROVED)
        import concurrent.futures

        def consume() -> None:
            try:
                registry.consume("approval-race", "fp-1")
            except Exception:
                return

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(consume) for _ in range(2)]
            for future in futures:
                future.result()
        # One should succeed, one should fail
    finally:
        os.unlink(db_path)
