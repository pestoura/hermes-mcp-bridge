"""Policy-as-code model for V2 Phase 1 — fail closed by construction.

Phase 1 decisions (partial, scoped answer to OD-017; the engine/format choice
for later phases stays open):

* rules are **explicit per ``policy_action``**; there is no wildcard, glob or
  prefix matching. A rule whose action contains ``*``/``?``/``[``/``]`` is
  rejected at construction time, so a permissive rule cannot be written;
* the outcome set is exactly ``ALLOW``, ``DENY``, ``APPROVAL_REQUIRED``;
* every non-ALLOW outcome carries a stable, non-secret reason code.

Fail-closed order of evaluation is fixed and must not be reordered: an unknown
tool can never reach the capability checks, and the T4/destructive backstop is
applied *before* an ALLOW rule can take effect.
"""

from __future__ import annotations

from enum import StrEnum, unique
from typing import Any

from pydantic import ConfigDict, field_validator

from ._models import PolicyModel, RegistryModel
from .capabilities import CapabilityRegistry
from .credentials import CredentialBroker
from .enums import ApprovalRequirement, PolicyDecision
from .errors import PolicyValidationError
from .registry import ToolRegistry
from .schema import ToolDefinition, normalize_identifier


@unique
class ReasonCode(StrEnum):
    """Stable reason codes. Never contain secrets, paths or arguments."""

    ALLOWED = "ALLOWED"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    MISSING_POLICY_RULE = "MISSING_POLICY_RULE"
    EXPLICIT_DENY = "EXPLICIT_DENY"
    APPROVAL_REQUIRED_BY_RULE = "APPROVAL_REQUIRED_BY_RULE"
    APPROVAL_REQUIRED_BY_TOOL = "APPROVAL_REQUIRED_BY_TOOL"
    CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
    CAPABILITY_NOT_READY = "CAPABILITY_NOT_READY"
    CREDENTIAL_CAPABILITY_UNKNOWN = "CREDENTIAL_CAPABILITY_UNKNOWN"
    CREDENTIAL_CAPABILITY_NOT_READY = "CREDENTIAL_CAPABILITY_NOT_READY"
    DESTRUCTIVE_DENIED_BY_DEFAULT = "DESTRUCTIVE_DENIED_BY_DEFAULT"
    POLICY_ACTION_MISMATCH = "POLICY_ACTION_MISMATCH"


_WILDCARD_CHARS = ("*", "?", "[", "]")


class PolicyRule(PolicyModel):
    """One explicit rule bound to exactly one ``policy_action``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_action: str
    decision: PolicyDecision
    note: str = ""

    @field_validator("policy_action")
    @classmethod
    def _action(cls, value: str) -> str:
        raw = value.strip()
        for char in _WILDCARD_CHARS:
            if char in raw:
                raise PolicyValidationError(
                    "permissive wildcard policy rules are rejected in Phase 1"
                )
        try:
            return normalize_identifier(raw, field="policy_action")
        except Exception as exc:
            raise PolicyValidationError(f"invalid policy_action: {exc}") from exc

    def canonical(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "note": self.note,
            "policy_action": self.policy_action,
        }


class PolicyRuleSet:
    """Immutable set of explicit rules, keyed by ``policy_action``."""

    __slots__ = ("_rules",)

    def __init__(self, rules: list[PolicyRule] | None = None) -> None:
        mapping: dict[str, PolicyRule] = {}
        for rule in rules or []:
            if rule.policy_action in mapping:
                raise PolicyValidationError(f"duplicate policy rule: {rule.policy_action}")
            mapping[rule.policy_action] = rule
        self._rules = mapping

    def get(self, policy_action: str) -> PolicyRule | None:
        try:
            key = normalize_identifier(policy_action, field="policy_action")
        except Exception:
            return None
        return self._rules.get(key)

    def __len__(self) -> int:
        return len(self._rules)

    def ordered(self) -> list[PolicyRule]:
        return [self._rules[key] for key in sorted(self._rules)]

    def canonical(self) -> list[dict[str, Any]]:
        return [rule.canonical() for rule in self.ordered()]


class PolicyEvaluation(RegistryModel):
    """Result of evaluating one tool. Safe to log and to serialize."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_id: str
    policy_action: str
    decision: PolicyDecision
    reason_code: ReasonCode

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW

    @property
    def requires_approval(self) -> bool:
        return self.decision is PolicyDecision.APPROVAL_REQUIRED

    def canonical(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "policy_action": self.policy_action,
            "reason_code": self.reason_code.value,
            "tool_id": self.tool_id,
        }


def _deny(tool_id: str, action: str, reason: ReasonCode) -> PolicyEvaluation:
    return PolicyEvaluation(
        tool_id=tool_id,
        policy_action=action,
        decision=PolicyDecision.DENY,
        reason_code=reason,
    )


class PolicyEngine:
    """Deterministic fail-closed policy evaluation for Phase 1."""

    __slots__ = ("_broker", "_registry", "_rules")

    def __init__(
        self,
        registry: ToolRegistry,
        rules: PolicyRuleSet,
        credential_broker: CredentialBroker | None = None,
    ) -> None:
        self._registry = registry
        self._rules = rules
        self._broker = credential_broker

    @property
    def rules(self) -> PolicyRuleSet:
        return self._rules

    @property
    def capabilities(self) -> CapabilityRegistry:
        return self._registry.capabilities

    def evaluate(self, tool_id: str) -> PolicyEvaluation:
        """Evaluate one tool by id. Unknown tools DENY without touching rules."""
        if not self._registry.contains(tool_id):
            safe_id = tool_id.strip().lower() if isinstance(tool_id, str) else "<invalid>"
            return PolicyEvaluation(
                tool_id=safe_id or "<empty>",
                policy_action="<unknown>",
                decision=PolicyDecision.DENY,
                reason_code=ReasonCode.UNKNOWN_TOOL,
            )
        return self.evaluate_tool(self._registry.get(tool_id))

    def evaluate_tool(self, tool: ToolDefinition) -> PolicyEvaluation:
        action = tool.policy_action
        tool_id = tool.tool_id

        # 1. destructive / T4 backstop — applied before any ALLOW rule can win.
        if tool.is_destructive:
            return _deny(tool_id, action, ReasonCode.DESTRUCTIVE_DENIED_BY_DEFAULT)

        # 2. capability must exist and be READY.
        capabilities = self._registry.capabilities
        if not capabilities.contains(tool.capability_id):
            return _deny(tool_id, action, ReasonCode.CAPABILITY_UNKNOWN)
        if not capabilities.get(tool.capability_id).is_ready:
            return _deny(tool_id, action, ReasonCode.CAPABILITY_NOT_READY)

        # 3. required credential capability must be READY.
        if tool.credential_capability_id is not None:
            if self._broker is None:
                return _deny(tool_id, action, ReasonCode.CREDENTIAL_CAPABILITY_UNKNOWN)
            status = self._broker.status(tool.credential_capability_id)
            if status is None:
                return _deny(tool_id, action, ReasonCode.CREDENTIAL_CAPABILITY_UNKNOWN)
            if not status.is_ready:
                return _deny(tool_id, action, ReasonCode.CREDENTIAL_CAPABILITY_NOT_READY)

        # 4. an explicit rule must exist for the action.
        rule = self._rules.get(action)
        if rule is None:
            return _deny(tool_id, action, ReasonCode.MISSING_POLICY_RULE)
        if rule.policy_action != action:  # defensive; normalization guarantees equality
            return _deny(tool_id, action, ReasonCode.POLICY_ACTION_MISMATCH)

        if rule.decision is PolicyDecision.DENY:
            return _deny(tool_id, action, ReasonCode.EXPLICIT_DENY)

        if rule.decision is PolicyDecision.APPROVAL_REQUIRED:
            return PolicyEvaluation(
                tool_id=tool_id,
                policy_action=action,
                decision=PolicyDecision.APPROVAL_REQUIRED,
                reason_code=ReasonCode.APPROVAL_REQUIRED_BY_RULE,
            )

        # 5. rule says ALLOW, but a tool declaring REQUIRED approval cannot be
        #    downgraded to a plain ALLOW.
        if tool.approval_requirement is ApprovalRequirement.REQUIRED:
            return PolicyEvaluation(
                tool_id=tool_id,
                policy_action=action,
                decision=PolicyDecision.APPROVAL_REQUIRED,
                reason_code=ReasonCode.APPROVAL_REQUIRED_BY_TOOL,
            )
        if tool.approval_requirement is ApprovalRequirement.CONDITIONAL:
            return PolicyEvaluation(
                tool_id=tool_id,
                policy_action=action,
                decision=PolicyDecision.APPROVAL_REQUIRED,
                reason_code=ReasonCode.APPROVAL_REQUIRED_BY_TOOL,
            )

        return PolicyEvaluation(
            tool_id=tool_id,
            policy_action=action,
            decision=PolicyDecision.ALLOW,
            reason_code=ReasonCode.ALLOWED,
        )

    def evaluate_all(self) -> list[PolicyEvaluation]:
        """Evaluate every registered tool, ordered by ``tool_id``."""
        return [self.evaluate_tool(tool) for tool in self._registry.ordered()]


__all__ = [
    "PolicyEngine",
    "PolicyEvaluation",
    "PolicyRule",
    "PolicyRuleSet",
    "ReasonCode",
]
