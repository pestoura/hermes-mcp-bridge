"""Canonical typed tool contract for Hermes Factory northbound control.

The Factory is an internal governed capability exposed to *external* clients
through the existing Hermes MCP Bridge. It is deliberately not modelled as a
provider lane: there is no provider credential, egress host, generic HTTP
surface or second MCP server here.

This module describes the closed V2 tool surface only. Runtime registration is
separate and remains disabled by default until controlled Factory installation.
"""

from __future__ import annotations

from typing import Any

from .capabilities import CapabilityDescriptor, CapabilityRegistry
from .enums import (
    ApprovalRequirement,
    CapabilityState,
    ExecutionMode,
    IdempotencySemantics,
    MutationClass,
    PolicyDecision,
    ResultShaping,
    RetryClass,
    SecurityTier,
    Stability,
)
from .policy import PolicyRule, PolicyRuleSet
from .registry import ToolRegistry
from .schema import ResourceKey, RetryPolicy, ToolDefinition

FACTORY_CONTROL_CAPABILITY = "factory.control"

FACTORY_NORTHBOUND_TOOL_IDS: tuple[str, ...] = (
    "factory.acceptance",
    "factory.evidence",
    "factory.protected_mutation_intent",
    "factory.status",
)

_PROTECTED_ACTIONS = [
    "ACTIVATE_PROFILE",
    "ACTIVATE_SKILL",
    "MERGE_PR",
    "RELEASE",
]


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _read_input_schema() -> dict[str, Any]:
    return _object_schema(
        {
            "candidate_sha": {"type": "string", "minLength": 1, "maxLength": 128},
            "principal": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        ["candidate_sha", "principal"],
    )


def _response_schema() -> dict[str, Any]:
    return _object_schema(
        {
            "schema_version": {"type": "string"},
            "operation": {"type": "string"},
            "candidate_sha": {"type": "string"},
            "data": {"type": "object", "additionalProperties": True},
        },
        ["schema_version", "operation", "candidate_sha", "data"],
    )


def _read_tool(*, tool_id: str, policy_action: str) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        provider="factory",
        operation=tool_id.split(".", 1)[1],
        execution_mode=ExecutionMode.DIRECT,
        input_schema=_read_input_schema(),
        output_schema=_response_schema(),
        security_tier=SecurityTier.T1,
        read_only=True,
        mutation_class=MutationClass.NONE,
        idempotency=IdempotencySemantics.READ,
        policy_action=policy_action,
        approval_requirement=ApprovalRequirement.NOT_REQUIRED,
        capability_id=FACTORY_CONTROL_CAPABILITY,
        credential_capability_id=None,
        timeout_seconds=10,
        retry_policy=RetryPolicy(retry_class=RetryClass.NO_RETRY, max_attempts=1),
        resource_key=ResourceKey(scope="factory", selector="candidate"),
        result_shaping=ResultShaping.REQUIRED,
        stability=Stability.EXPERIMENTAL,
        backend="factory-control",
    )


def factory_northbound_definitions() -> list[ToolDefinition]:
    """Return the closed Factory northbound definitions in stable order."""
    read_tools = [
        _read_tool(tool_id="factory.acceptance", policy_action="factory.acceptance.read"),
        _read_tool(tool_id="factory.evidence", policy_action="factory.evidence.read"),
        _read_tool(tool_id="factory.status", policy_action="factory.status.read"),
    ]
    mutation = ToolDefinition(
        tool_id="factory.protected_mutation_intent",
        provider="factory",
        operation="protected_mutation_intent",
        execution_mode=ExecutionMode.DIRECT,
        input_schema=_object_schema(
            {
                "candidate_sha": {"type": "string", "minLength": 1, "maxLength": 128},
                "principal": {"type": "string", "minLength": 1, "maxLength": 200},
                "action": {"type": "string", "enum": list(_PROTECTED_ACTIONS)},
                "resource": {"type": "string", "minLength": 1, "maxLength": 500},
                "authority_evidence_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                },
                "human_decision_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                },
            },
            [
                "candidate_sha",
                "principal",
                "action",
                "resource",
                "authority_evidence_id",
                "human_decision_id",
            ],
        ),
        output_schema=_response_schema(),
        security_tier=SecurityTier.T3,
        read_only=False,
        mutation_class=MutationClass.PRIVILEGED,
        idempotency=IdempotencySemantics.NATURALLY_IDEMPOTENT,
        policy_action="factory.protected_mutation.prepare",
        approval_requirement=ApprovalRequirement.REQUIRED,
        capability_id=FACTORY_CONTROL_CAPABILITY,
        credential_capability_id=None,
        timeout_seconds=10,
        retry_policy=RetryPolicy(retry_class=RetryClass.NO_RETRY, max_attempts=1),
        resource_key=ResourceKey(scope="factory", selector="candidate"),
        result_shaping=ResultShaping.REQUIRED,
        stability=Stability.EXPERIMENTAL,
        backend="factory-control",
    )
    return sorted([*read_tools, mutation], key=lambda tool: tool.tool_id)


def build_factory_northbound_registry(
    *,
    state: CapabilityState = CapabilityState.READY,
) -> ToolRegistry:
    """Build the frozen typed registry for the optional Factory boundary."""
    capabilities = CapabilityRegistry(
        [
            CapabilityDescriptor(
                capability_id=FACTORY_CONTROL_CAPABILITY,
                provider="factory",
                state=state,
                description="Hermes Software Factory external governance/control boundary.",
            )
        ]
    )
    return ToolRegistry(capabilities, factory_northbound_definitions()).freeze()


def factory_northbound_policy_rules() -> PolicyRuleSet:
    """Explicit rules; protected mutation still requires approval by tool contract."""
    return PolicyRuleSet(
        [
            PolicyRule(policy_action=action, decision=PolicyDecision.ALLOW)
            for action in (
                "factory.acceptance.read",
                "factory.evidence.read",
                "factory.protected_mutation.prepare",
                "factory.status.read",
            )
        ]
    )


__all__ = [
    "FACTORY_CONTROL_CAPABILITY",
    "FACTORY_NORTHBOUND_TOOL_IDS",
    "build_factory_northbound_registry",
    "factory_northbound_definitions",
    "factory_northbound_policy_rules",
]
