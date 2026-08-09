"""Phase 9 hardening: audit chain and cardinality bound acceptance tests."""

from __future__ import annotations

import pytest

from hermes_mcp_bridge.v2.audit_chain import (
    MAX_NODES_PER_REQUEST,
    MAX_REASON_LABELS_PER_RUN,
    AuditChainError,
    bind_audit_link,
    digest_chain,
    enforce_cardinality,
    link_reason_code,
    truncated_rejected_branches,
)
from hermes_mcp_bridge.v2.enums import PolicyDecision
from hermes_mcp_bridge.v2.resolver import ModeResolver
from hermes_mcp_bridge.v2.resolver_contract import (
    IntentOperation,
    ResolverBudget,
    ResolverIntent,
    ResolverReason,
)


def _intent(operations=(), **kw):
    return ResolverIntent(
        request_id=kw.pop("request_id", "audit"),
        principal_ref="audit",
        operations=tuple(operations),
        **kw,
    )


def _op(capability_id="github.repo_read", ref="", **kw):
    return IntentOperation(
        capability_id=capability_id,
        target_scope_ref=kw.pop("target_scope_ref", "pestoura/hermes-mcp-bridge"),
        operation_ref=ref or capability_id,
        **kw,
    )


def _resolver():
    from hermes_mcp_bridge.v2.enums import CapabilityState

    return ModeResolver(
        snapshot={"github.repo_read": CapabilityState.READY},
        snapshot_digest="a" * 64,
        budget=ResolverBudget(agentic_token_budget=1_000),
        write_capabilities=frozenset(),
    )


def test_audit_link_binds_when_executed_plan_matches_decision() -> None:
    intent = _intent((_op(),))
    decision = _resolver().resolve(intent, policy=PolicyDecision.ALLOW)
    link = bind_audit_link(decision, intent)
    assert link.matched is True
    assert link.decision_digest == decision.digest()
    assert link.executed_plan_digest == intent.digest()
    assert link.mode == "DIRECT"
    assert link.reason_code == "R-DIRECT-EXACT"
    # Re-binding is deterministic.
    assert bind_audit_link(decision, intent).digest() == link.digest()


def test_audit_link_fails_closed_when_executed_plan_diverges() -> None:
    intent = _intent((_op(),))
    decision = _resolver().resolve(intent, policy=PolicyDecision.ALLOW)
    # Tampered executed intent: a different target ref changes the digest.
    tampered = _intent((_op(target_scope_ref="other/repo"),))
    link = bind_audit_link(decision, tampered)
    assert link.matched is False
    assert link.executed_plan_digest != decision.intent_digest
    # The gate must refuse on a non-matching link.
    assert link.matched is False


def test_cardinality_enforced_on_node_count() -> None:
    over = _intent([_op(ref=f"o{i}") for i in range(MAX_NODES_PER_REQUEST + 1)])
    with pytest.raises(AuditChainError):
        enforce_cardinality(over, budget_nodes=MAX_NODES_PER_REQUEST)
    within = _intent([_op(ref=f"o{i}") for i in range(10)])
    enforce_cardinality(within, budget_nodes=MAX_NODES_PER_REQUEST)  # no raise


def test_cardinality_enforced_on_budget() -> None:
    intent = _intent([_op(ref=f"o{i}") for i in range(5)])
    with pytest.raises(AuditChainError):
        enforce_cardinality(intent, budget_nodes=3)


def test_cardinality_enforced_on_escalations() -> None:
    intent = _intent((), no_contract_coverage=True, escalation_count=2)
    with pytest.raises(AuditChainError):
        enforce_cardinality(intent, budget_nodes=MAX_NODES_PER_REQUEST)


def test_rejected_branch_labels_are_bounded() -> None:
    # Use the real rejection codes from the closed enumeration to prove the
    # label set stays bounded even at the enumeration's natural ceiling.
    from hermes_mcp_bridge.v2.resolver_contract import ResolverDecision

    real_rejections = [reason for reason in ResolverReason if reason.is_rejection]
    decision = ResolverDecision(
        request_id="r",
        mode=None,
        primary_reason_code=ResolverReason.E_AGENTIC_NOT_ALLOWED,
        rejected_branches=tuple(real_rejections),
    )
    labels = truncated_rejected_branches(decision)
    assert len(labels) <= MAX_REASON_LABELS_PER_RUN


def test_single_bounded_metric_label_is_always_in_enum() -> None:
    intent = _intent((_op(),))
    decision = _resolver().resolve(intent, policy=PolicyDecision.ALLOW)
    label = link_reason_code(decision)
    assert label in {reason.value for reason in ResolverReason}


def test_digest_chain_detects_tampering() -> None:
    a = {"x": 1}
    b = {"y": 2}
    chain1 = digest_chain(a, b)
    chain2 = digest_chain(a, {"y": 3})  # tampered second link
    assert chain1 != chain2
    # Reordering breaks the chain too (order matters).
    assert digest_chain(b, a) != chain1


def test_audit_chain_does_not_leak_secrets() -> None:
    intent = _intent((_op(target_scope_ref="pestoura/hermes-mcp-bridge"),))
    decision = _resolver().resolve(intent, policy=PolicyDecision.ALLOW)
    link = bind_audit_link(decision, intent)
    serialized = str(link.canonical())
    assert "ghp_" not in serialized and "xoxb-" not in serialized
    assert "/home/" not in serialized


def test_cardinality_bounds_are_consistent() -> None:
    from hermes_mcp_bridge.v2.audit_chain import MAX_REJECTED_BRANCHES

    assert MAX_REASON_LABELS_PER_RUN == 1 + MAX_REJECTED_BRANCHES
