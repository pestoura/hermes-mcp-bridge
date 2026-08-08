"""Canonical registry entries for the Phase 2 GitHub DIRECT read-only MVP.

This module only describes typed capabilities. It does not expose MCP tools and
it does not imply that a Jarvas-side GitHub credential is configured. Connected
provider discovery and shadow evidence remain required before
``DIRECT_READ_ACCEPTED``.
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

GITHUB_API_CAPABILITY = "github.api"
GITHUB_READ_CREDENTIAL_CAPABILITY = "github.read"

GITHUB_DIRECT_READ_TOOL_IDS = (
    "github.get_checks",
    "github.get_issue",
    "github.get_pr",
    "github.get_repo",
    "github.search",
)


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _common_repo_properties() -> dict[str, Any]:
    return {
        "owner": {"type": "string", "minLength": 1, "maxLength": 100},
        "repo": {"type": "string", "minLength": 1, "maxLength": 100},
        "select": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 32,
            "uniqueItems": True,
        },
    }


def _tool(
    *,
    tool_id: str,
    policy_action: str,
    input_schema: dict[str, Any],
) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        provider="github",
        operation=tool_id.split(".", 1)[1],
        execution_mode=ExecutionMode.DIRECT,
        input_schema=input_schema,
        output_schema={"type": "object", "additionalProperties": True},
        security_tier=SecurityTier.T1,
        read_only=True,
        mutation_class=MutationClass.NONE,
        idempotency=IdempotencySemantics.READ,
        policy_action=policy_action,
        approval_requirement=ApprovalRequirement.NOT_REQUIRED,
        capability_id=GITHUB_API_CAPABILITY,
        credential_capability_id=GITHUB_READ_CREDENTIAL_CAPABILITY,
        timeout_seconds=30,
        retry_policy=RetryPolicy(retry_class=RetryClass.NO_RETRY, max_attempts=1),
        resource_key=ResourceKey(scope="repository", selector="default"),
        result_shaping=ResultShaping.REQUIRED,
        stability=Stability.EXPERIMENTAL,
        backend="github-rest",
    )


def github_direct_read_definitions() -> list[ToolDefinition]:
    """Return the five typed Phase 2 read-only definitions in stable order."""
    repo_props = _common_repo_properties()

    get_repo = _tool(
        tool_id="github.get_repo",
        policy_action="github.repo.read",
        input_schema=_object_schema(dict(repo_props), ["owner", "repo"]),
    )

    numbered_props = dict(repo_props)
    numbered_props["number"] = {"type": "integer", "minimum": 1}
    get_pr = _tool(
        tool_id="github.get_pr",
        policy_action="github.pr.read",
        input_schema=_object_schema(dict(numbered_props), ["owner", "repo", "number"]),
    )
    get_issue = _tool(
        tool_id="github.get_issue",
        policy_action="github.issue.read",
        input_schema=_object_schema(dict(numbered_props), ["owner", "repo", "number"]),
    )

    checks_props = dict(repo_props)
    checks_props.update(
        {
            "ref": {"type": "string", "minLength": 1, "maxLength": 200},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 100},
        }
    )
    get_checks = _tool(
        tool_id="github.get_checks",
        policy_action="github.checks.read",
        input_schema=_object_schema(checks_props, ["owner", "repo", "ref"]),
    )

    search_props = dict(repo_props)
    search_props.update(
        {
            "text": {"type": "string", "minLength": 1, "maxLength": 200},
            "item_type": {"type": "string", "enum": ["issue", "pr", "any"]},
            "state": {"type": "string", "enum": ["open", "closed", "any"]},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 30},
        }
    )
    search = _tool(
        tool_id="github.search",
        policy_action="github.search.read",
        input_schema=_object_schema(search_props, ["owner", "repo", "text"]),
    )

    return sorted(
        [get_repo, get_pr, get_issue, get_checks, search],
        key=lambda tool: tool.tool_id,
    )


def build_github_direct_read_registry(
    *,
    api_state: CapabilityState = CapabilityState.READY,
    credential_state: CapabilityState = CapabilityState.READY,
) -> ToolRegistry:
    """Build a frozen registry for the five Phase 2 GitHub read capabilities."""
    capabilities = CapabilityRegistry(
        [
            CapabilityDescriptor(
                capability_id=GITHUB_API_CAPABILITY,
                provider="github",
                state=api_state,
                description="GitHub REST API connectivity for typed V2 operations.",
            ),
            CapabilityDescriptor(
                capability_id=GITHUB_READ_CREDENTIAL_CAPABILITY,
                provider="github",
                state=credential_state,
                description="Least-privilege GitHub read authorization capability.",
            ),
        ]
    )
    return ToolRegistry(capabilities, github_direct_read_definitions()).freeze()


def github_direct_read_policy_rules() -> PolicyRuleSet:
    """Explicit allow rules for the five read-only actions; no wildcard authority."""
    return PolicyRuleSet(
        [
            PolicyRule(policy_action=action, decision=PolicyDecision.ALLOW)
            for action in (
                "github.checks.read",
                "github.issue.read",
                "github.pr.read",
                "github.repo.read",
                "github.search.read",
            )
        ]
    )


__all__ = [
    "GITHUB_API_CAPABILITY",
    "GITHUB_DIRECT_READ_TOOL_IDS",
    "GITHUB_READ_CREDENTIAL_CAPABILITY",
    "build_github_direct_read_registry",
    "github_direct_read_definitions",
    "github_direct_read_policy_rules",
]
