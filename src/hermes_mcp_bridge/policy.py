"""Policy engine: deterministic evaluation of allow/deny/require-approval rules."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from .protocol import (
    DecisionType,
    MutationClass,
    PolicyEvaluationInput,
    PolicyEvaluationResult,
    TrustLabel,
)

_POLICY_SECRET = (
    os.environ.get("HERMES_BRIDGE_POLICY_SECRET")
    or os.environ.get("HERMES_BRIDGE_HMAC_SECRET")
)


class PolicyError(Exception):
    """Invalid policy configuration."""


def _load_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return {}
    return policy


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


def _contains(values: list[str], target: str) -> bool:
    target_norm = target.strip().lower()
    return any(item.strip().lower() == target_norm for item in values)


def _trust_risk(trust_label: TrustLabel) -> str:
    if trust_label in {TrustLabel.TRUSTED_POLICY, TrustLabel.USER_INSTRUCTION}:
        return "low"
    if trust_label == TrustLabel.AGENT_PROPOSAL:
        return "medium"
    if trust_label == TrustLabel.TOOL_RESULT:
        return "low"
    if trust_label == TrustLabel.UNTRUSTED_CONTENT:
        return "high"
    return "unknown"


def evaluate_policy(
    evaluation: PolicyEvaluationInput,
    *,
    policy: dict[str, Any] | None = None,
) -> PolicyEvaluationResult:
    loaded = _load_policy(policy)
    default_policy = loaded.get("default", {})
    if not isinstance(default_policy, dict):
        raise PolicyError("policy.default must be a mapping")

    effective_policy: dict[str, Any] = {"source": "bridge", "default": default_policy}

    action = str(evaluation.action or "").strip()
    if not action:
        return PolicyEvaluationResult(
            decision=DecisionType.DENY,
            reason="missing action",
            effective_policy=effective_policy,
        )

    mutation_class = evaluation.mutation_class
    read_actions = {"read", "status", "health", "list", "manifest"}
    if mutation_class == MutationClass.NONE and action not in read_actions:
        mutation_class = MutationClass.WRITE

    # Default-deny posture
    decision = DecisionType.ALLOW
    reason = "default allow"

    deny_actions = _as_list(loaded.get("deny_actions"))
    if deny_actions and _contains(deny_actions, action):
        decision = DecisionType.DENY
        reason = "deny_actions"

    require_approval_actions = _as_list(loaded.get("require_approval_actions"))
    if (
        decision == DecisionType.ALLOW
        and require_approval_actions
        and _contains(require_approval_actions, action)
    ):
        decision = DecisionType.REQUIRE_APPROVAL
        reason = "require_approval_actions"

    if (
        decision == DecisionType.ALLOW
        and mutation_class
        in {MutationClass.WRITE, MutationClass.DELETE, MutationClass.ADMIN}
    ):
        decision = DecisionType.REQUIRE_APPROVAL
        reason = f"mutation class {mutation_class.value} requires approval by default"

    risk = _trust_risk(evaluation.trust_label)
    effective_policy["trust_risk"] = risk
    effective_policy["mutation_class"] = mutation_class.value
    effective_policy["action"] = action

    if (
        decision == DecisionType.ALLOW
        and risk == "high"
        and mutation_class != MutationClass.NONE
    ):
        decision = DecisionType.REQUIRE_APPROVAL
        reason = "high-risk trust label with mutation"

    return PolicyEvaluationResult(
        decision=decision,
        reason=reason,
        effective_policy=effective_policy,
        approval_required=decision == DecisionType.REQUIRE_APPROVAL,
    )


def deterministic_policy_signature(
    evaluation: PolicyEvaluationInput,
    result: PolicyEvaluationResult,
) -> str:
    payload = {
        "action": evaluation.action,
        "origin_type": evaluation.origin_type,
        "project_key": evaluation.project_key,
        "resource": evaluation.resource,
        "trust_label": evaluation.trust_label.value,
        "mutation_class": evaluation.mutation_class.value,
        "principal": evaluation.principal,
        "delegation_chain": evaluation.delegation_chain,
        "decision": result.decision.value,
        "reason": result.reason,
    }
    normalized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    if not _POLICY_SECRET:
        return f"unsigned:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"
    return hmac.new(
        _POLICY_SECRET.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
