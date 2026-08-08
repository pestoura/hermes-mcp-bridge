"""Phase 1 canonical registry core tests.

Covers the 18 required scenarios for the V2 registry slice. Every test is
in-process and hermetic: no filesystem, no network, no V1 runtime.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hermes_mcp_bridge import contracts
from hermes_mcp_bridge.v2 import (
    ApprovalRequirement,
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityState,
    CredentialBroker,
    CredentialCapabilityStatus,
    DuplicateCapabilityError,
    ExecutionMode,
    IdempotencySemantics,
    MutationClass,
    PolicyDecision,
    PolicyEngine,
    PolicyRule,
    PolicyRuleSet,
    ProjectionContext,
    RegistryValidationError,
    ResourceKey,
    RetryPolicy,
    SecurityTier,
    StaticCredentialBroker,
    ToolDefinition,
    ToolRegistry,
    UnknownCapabilityError,
    UnknownToolError,
    project_capabilities,
)
from hermes_mcp_bridge.v2.canonical import canonical_json_text
from hermes_mcp_bridge.v2.enums import RetryClass
from hermes_mcp_bridge.v2.errors import DuplicateToolError, PolicyValidationError
from hermes_mcp_bridge.v2.policy import ReasonCode

# Sentinel values used to prove secrets never reach any serialization.
SECRET_SENTINEL = "ZZZSECRETVALUEZZZ"
SECRET_PATH_SENTINEL = "/var/lib/zzz-secret-path"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _schema() -> dict[str, Any]:
    return {"type": "object", "properties": {"number": {"type": "integer"}}}


def read_tool(
    tool_id: str = "github.get_pr",
    *,
    capability_id: str = "github.api",
    policy_action: str = "github.pr.read",
    credential_capability_id: str | None = None,
    approval: ApprovalRequirement = ApprovalRequirement.NOT_REQUIRED,
    version: int = 1,
) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        provider="github",
        operation=tool_id.split(".", 1)[1],
        version=version,
        execution_mode=ExecutionMode.DIRECT,
        input_schema=_schema(),
        output_schema=_schema(),
        security_tier=SecurityTier.T0,
        read_only=True,
        mutation_class=MutationClass.NONE,
        idempotency=IdempotencySemantics.READ,
        policy_action=policy_action,
        approval_requirement=approval,
        capability_id=capability_id,
        credential_capability_id=credential_capability_id,
        timeout_seconds=30,
        retry_policy=RetryPolicy(retry_class=RetryClass.RETRY_SAFE, max_attempts=3),
        resource_key=ResourceKey(scope="repository", selector="default"),
        description="Read one pull request.",
        backend="github-api",
    )


def mutation_tool(
    tool_id: str = "github.create_pr",
    *,
    tier: SecurityTier = SecurityTier.T2,
    mutation: MutationClass = MutationClass.STANDARD,
    policy_action: str = "github.pr.create",
    capability_id: str = "github.api",
    credential_capability_id: str | None = None,
    approval: ApprovalRequirement = ApprovalRequirement.NOT_REQUIRED,
) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        provider="github",
        operation=tool_id.split(".", 1)[1],
        execution_mode=ExecutionMode.DIRECT,
        input_schema=_schema(),
        output_schema=_schema(),
        security_tier=tier,
        read_only=False,
        mutation_class=mutation,
        idempotency=IdempotencySemantics.KEYED_IDEMPOTENT,
        policy_action=policy_action,
        approval_requirement=approval,
        capability_id=capability_id,
        credential_capability_id=credential_capability_id,
        timeout_seconds=60,
        resource_key=ResourceKey(scope="repository", selector="default"),
    )


def capability(
    capability_id: str = "github.api",
    state: CapabilityState = CapabilityState.READY,
    provider: str = "github",
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id, provider=provider, state=state
    )


def registry_with(
    tools: list[ToolDefinition],
    capabilities: list[CapabilityDescriptor] | None = None,
) -> ToolRegistry:
    caps = CapabilityRegistry(capabilities or [capability()])
    return ToolRegistry(caps, tools)


def allow_rules(*actions: str) -> PolicyRuleSet:
    return PolicyRuleSet(
        [PolicyRule(policy_action=a, decision=PolicyDecision.ALLOW) for a in actions]
    )


# ---------------------------------------------------------------------------
# 1. valid schema and invalid invariants
# ---------------------------------------------------------------------------


def test_valid_tool_definition_normalizes_and_validates() -> None:
    tool = read_tool()
    assert tool.tool_id == "github.get_pr"
    assert tool.security_tier is SecurityTier.T0
    assert tool.read_only is True
    assert tool.mutation_class is MutationClass.NONE
    assert len(tool.definition_hash()) == 64


def test_identifier_normalization_strips_and_lowercases() -> None:
    tool = read_tool().model_copy()
    normalized = ToolDefinition(
        **{**read_tool().model_dump(), "tool_id": "  GitHub.Get_PR  ", "provider": " GitHub "}
    )
    assert normalized.tool_id == "github.get_pr"
    assert normalized.provider == "github"
    assert tool.tool_id == normalized.tool_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool_id": ""},
        {"policy_action": "  "},
        {"provider": ""},
        {"capability_id": ""},
    ],
)
def test_empty_identifiers_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), **overrides})


def test_read_only_requires_mutation_none() -> None:
    with pytest.raises(RegistryValidationError):
        ToolDefinition(
            **{
                **read_tool().model_dump(),
                "mutation_class": MutationClass.STANDARD,
            }
        )


def test_mutating_tool_must_not_declare_mutation_none() -> None:
    with pytest.raises(RegistryValidationError):
        ToolDefinition(
            **{
                **mutation_tool().model_dump(),
                "mutation_class": MutationClass.NONE,
            }
        )


@pytest.mark.parametrize("timeout", [0, -1, 100000])
def test_timeout_must_be_bounded_and_positive(timeout: int) -> None:
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "timeout_seconds": timeout})


@pytest.mark.parametrize("schema", [None, [], "object", {}, {"type": "string"}])
def test_schemas_must_be_json_objects(schema: Any) -> None:
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "input_schema": schema})
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "output_schema": schema})


def test_wildcard_in_policy_action_rejected_on_tool() -> None:
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "policy_action": "github.read.*"})


def test_tool_id_must_be_namespaced_by_provider() -> None:
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "tool_id": "gitlab.get_pr"})


def test_secretish_backend_metadata_rejected() -> None:
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "backend": "github-api token"})


def test_destructive_requires_t4_and_vice_versa() -> None:
    with pytest.raises(RegistryValidationError):
        mutation_tool(tier=SecurityTier.T2, mutation=MutationClass.DESTRUCTIVE)
    with pytest.raises(RegistryValidationError):
        mutation_tool(tier=SecurityTier.T4, mutation=MutationClass.STANDARD)


def test_non_idempotent_mutation_cannot_be_retry_safe() -> None:
    base = mutation_tool().model_dump()
    base["idempotency"] = IdempotencySemantics.NON_IDEMPOTENT
    base["retry_policy"] = RetryPolicy(retry_class=RetryClass.RETRY_SAFE, max_attempts=3)
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**base)


def test_unknown_capability_reference_rejected() -> None:
    caps = CapabilityRegistry([capability("github.api")])
    registry = ToolRegistry(caps)
    with pytest.raises(RegistryValidationError):
        registry.register(read_tool(capability_id="github.absent"))


def test_unknown_credential_capability_reference_rejected() -> None:
    caps = CapabilityRegistry([capability("github.api")])
    registry = ToolRegistry(caps)
    with pytest.raises(RegistryValidationError):
        registry.register(read_tool(credential_capability_id="github.absent"))


# ---------------------------------------------------------------------------
# 2. duplicates
# ---------------------------------------------------------------------------


def test_duplicate_tool_rejected() -> None:
    registry = registry_with([read_tool()])
    with pytest.raises(DuplicateToolError):
        registry.register(read_tool())


def test_duplicate_capability_rejected() -> None:
    caps = CapabilityRegistry([capability()])
    with pytest.raises(DuplicateCapabilityError):
        caps.register(capability())


def test_fail_closed_lookup() -> None:
    registry = registry_with([read_tool()])
    with pytest.raises(UnknownToolError):
        registry.get("github.absent")
    with pytest.raises(UnknownToolError):
        registry.get("not a valid id!")
    with pytest.raises(UnknownCapabilityError):
        registry.capabilities.get("github.absent")
    assert registry.capabilities.is_ready("github.absent") is False


# ---------------------------------------------------------------------------
# 3. CapabilityState semantics
# ---------------------------------------------------------------------------


EXPECTED_STATE_MATRIX = {
    CapabilityState.CONFIGURED: (True, False, False, False),
    CapabilityState.AVAILABLE: (True, True, False, False),
    CapabilityState.HEALTHY: (True, True, True, False),
    CapabilityState.READY: (True, True, True, True),
    CapabilityState.DEGRADED: (True, True, False, False),
    CapabilityState.UNAVAILABLE: (True, False, False, False),
    CapabilityState.DENIED: (True, False, False, False),
}


def test_capability_state_members_are_exactly_the_seven_required() -> None:
    assert set(CapabilityState) == set(EXPECTED_STATE_MATRIX)


@pytest.mark.parametrize("state", list(EXPECTED_STATE_MATRIX))
def test_capability_state_semantics(state: CapabilityState) -> None:
    configured, available, healthy, ready = EXPECTED_STATE_MATRIX[state]
    assert state.is_configured is configured
    assert state.is_available is available
    assert state.is_healthy is healthy
    assert state.is_ready is ready
    descriptor = capability(state=state)
    assert descriptor.is_configured is configured
    assert descriptor.is_available is available
    assert descriptor.is_healthy is healthy
    assert descriptor.is_ready is ready


def test_configured_available_healthy_ready_are_distinct() -> None:
    assert CapabilityState.CONFIGURED.is_available is False
    assert CapabilityState.AVAILABLE.is_healthy is False
    assert CapabilityState.HEALTHY.is_ready is False
    assert CapabilityState.DEGRADED.is_available is True
    assert CapabilityState.DEGRADED.is_healthy is False
    assert CapabilityState.DENIED.is_denied is True


def test_security_tiers_are_exactly_t0_to_t4() -> None:
    assert [tier.value for tier in SecurityTier] == ["T0", "T1", "T2", "T3", "T4"]


def test_execution_modes_cover_all_documented_modes() -> None:
    assert {mode.value for mode in ExecutionMode} == {
        "DIRECT",
        "BATCH",
        "DAG",
        "RUNBOOK",
        "HYBRID",
        "AGENTIC",
    }


# ---------------------------------------------------------------------------
# 4/5. deterministic snapshot
# ---------------------------------------------------------------------------


def _two_capability_registry() -> list[CapabilityDescriptor]:
    return [capability("github.api"), capability("system.local", provider="system")]


def test_snapshot_is_insertion_order_independent() -> None:
    tools_a = [read_tool("github.get_pr"), read_tool("github.get_repo")]
    tools_b = list(reversed(tools_a))
    caps_a = _two_capability_registry()
    caps_b = list(reversed(caps_a))

    snap_a = ToolRegistry(CapabilityRegistry(caps_a), tools_a).snapshot()
    snap_b = ToolRegistry(CapabilityRegistry(caps_b), tools_b).snapshot()

    assert snap_a.canonical_json == snap_b.canonical_json
    assert snap_a.capability_snapshot_hash == snap_b.capability_snapshot_hash
    assert snap_a == snap_b


def test_snapshot_hash_is_lowercase_64_hex() -> None:
    digest = registry_with([read_tool()]).capability_snapshot_hash()
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)


def test_snapshot_is_stable_across_repeated_calls() -> None:
    registry = registry_with([read_tool()])
    assert registry.capability_snapshot_hash() == registry.capability_snapshot_hash()


def test_snapshot_serialization_is_canonical_json() -> None:
    snapshot = registry_with([read_tool()]).snapshot()
    text = snapshot.canonical_json
    assert ", " not in text
    assert '": ' not in text
    assert json.loads(text) == snapshot.payload
    assert text == canonical_json_text(snapshot.payload)


def test_snapshot_contains_no_timestamps_or_paths() -> None:
    text = registry_with([read_tool()]).snapshot().canonical_json.lower()
    for token in ("timestamp", "created_at", "updated_at", "/home/", "/var/", "hostname"):
        assert token not in text


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda t: t.model_copy(update={"version": 2}), id="version"),
        pytest.param(
            lambda t: t.model_copy(update={"timeout_seconds": 31}), id="timeout"
        ),
        pytest.param(
            lambda t: t.model_copy(
                update={"input_schema": {"type": "object", "properties": {"x": {}}}}
            ),
            id="schema",
        ),
        pytest.param(
            lambda t: t.model_copy(update={"security_tier": SecurityTier.T1}), id="tier"
        ),
        pytest.param(
            lambda t: t.model_copy(update={"policy_action": "github.pr.read_alt"}),
            id="policy_action",
        ),
    ],
)
def test_material_change_changes_snapshot_hash(mutate: Any) -> None:
    base = read_tool()
    baseline = registry_with([base]).capability_snapshot_hash()
    changed = registry_with([mutate(base)]).capability_snapshot_hash()
    assert changed != baseline


def test_capability_state_change_changes_snapshot_hash() -> None:
    tool = read_tool()
    ready = registry_with([tool], [capability(state=CapabilityState.READY)])
    degraded = registry_with([tool], [capability(state=CapabilityState.DEGRADED)])
    assert ready.capability_snapshot_hash() != degraded.capability_snapshot_hash()


def test_adding_a_tool_changes_snapshot_hash() -> None:
    one = registry_with([read_tool("github.get_pr")])
    two = registry_with([read_tool("github.get_pr"), read_tool("github.get_repo")])
    assert one.capability_snapshot_hash() != two.capability_snapshot_hash()


# ---------------------------------------------------------------------------
# 6-14. policy decisions
# ---------------------------------------------------------------------------


def test_policy_allow() -> None:
    registry = registry_with([read_tool()])
    engine = PolicyEngine(registry, allow_rules("github.pr.read"))
    result = engine.evaluate("github.get_pr")
    assert result.decision is PolicyDecision.ALLOW
    assert result.reason_code is ReasonCode.ALLOWED
    assert result.allowed is True


def test_policy_explicit_deny() -> None:
    registry = registry_with([read_tool()])
    rules = PolicyRuleSet(
        [PolicyRule(policy_action="github.pr.read", decision=PolicyDecision.DENY)]
    )
    result = PolicyEngine(registry, rules).evaluate("github.get_pr")
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code is ReasonCode.EXPLICIT_DENY


def test_policy_require_approval_by_rule() -> None:
    registry = registry_with([read_tool()])
    rules = PolicyRuleSet(
        [
            PolicyRule(
                policy_action="github.pr.read", decision=PolicyDecision.APPROVAL_REQUIRED
            )
        ]
    )
    result = PolicyEngine(registry, rules).evaluate("github.get_pr")
    assert result.decision is PolicyDecision.APPROVAL_REQUIRED
    assert result.reason_code is ReasonCode.APPROVAL_REQUIRED_BY_RULE
    assert result.allowed is False


def test_tool_declaring_required_approval_cannot_be_plain_allow() -> None:
    tool = read_tool(approval=ApprovalRequirement.REQUIRED)
    registry = registry_with([tool])
    result = PolicyEngine(registry, allow_rules("github.pr.read")).evaluate("github.get_pr")
    assert result.decision is PolicyDecision.APPROVAL_REQUIRED
    assert result.reason_code is ReasonCode.APPROVAL_REQUIRED_BY_TOOL


def test_conditional_approval_defers_to_the_rule_and_can_allow() -> None:
    """CONDITIONAL is not REQUIRED: an explicit ALLOW rule resolves to ALLOW."""
    tool = read_tool(approval=ApprovalRequirement.CONDITIONAL)
    registry = registry_with([tool])
    result = PolicyEngine(registry, allow_rules("github.pr.read")).evaluate("github.get_pr")
    assert result.decision is PolicyDecision.ALLOW
    assert result.reason_code is ReasonCode.ALLOWED


def test_conditional_approval_requires_approval_under_an_approval_rule() -> None:
    tool = read_tool(approval=ApprovalRequirement.CONDITIONAL)
    registry = registry_with([tool])
    rules = PolicyRuleSet(
        [
            PolicyRule(
                policy_action="github.pr.read", decision=PolicyDecision.APPROVAL_REQUIRED
            )
        ]
    )
    result = PolicyEngine(registry, rules).evaluate("github.get_pr")
    assert result.decision is PolicyDecision.APPROVAL_REQUIRED
    assert result.reason_code is ReasonCode.APPROVAL_REQUIRED_BY_RULE


def test_conditional_and_required_are_not_semantically_equal() -> None:
    """Under the same ALLOW rule the two enum values must diverge."""
    registry_conditional = registry_with(
        [read_tool(approval=ApprovalRequirement.CONDITIONAL)]
    )
    registry_required = registry_with([read_tool(approval=ApprovalRequirement.REQUIRED)])
    rules = allow_rules("github.pr.read")

    conditional = PolicyEngine(registry_conditional, rules).evaluate("github.get_pr")
    required = PolicyEngine(registry_required, rules).evaluate("github.get_pr")
    assert conditional.decision is PolicyDecision.ALLOW
    assert required.decision is PolicyDecision.APPROVAL_REQUIRED
    assert conditional.decision is not required.decision


def test_conditional_does_not_bypass_the_destructive_backstop() -> None:
    """The T4/destructive DENY still runs before any approval reasoning."""
    tool = mutation_tool(
        tier=SecurityTier.T4,
        mutation=MutationClass.DESTRUCTIVE,
        approval=ApprovalRequirement.CONDITIONAL,
    )
    registry = registry_with([tool])
    result = PolicyEngine(registry, allow_rules("github.pr.create")).evaluate(
        "github.create_pr"
    )
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code is ReasonCode.DESTRUCTIVE_DENIED_BY_DEFAULT


def test_policy_rule_note_is_excluded_from_canonical_form() -> None:
    """A human note must not become a persistence channel for secrets."""
    rule = PolicyRule(
        policy_action="github.pr.read",
        decision=PolicyDecision.ALLOW,
        note=f"reviewed by ops {SECRET_SENTINEL}",
    )
    canonical = rule.canonical()
    assert "note" not in canonical
    assert canonical == {"decision": "ALLOW", "policy_action": "github.pr.read"}
    assert SECRET_SENTINEL not in canonical_json_text(canonical)
    assert SECRET_SENTINEL not in canonical_json_text(PolicyRuleSet([rule]).canonical())
    # the note is still available in-process for operators
    assert SECRET_SENTINEL in rule.note


def test_policy_missing_rule_denies() -> None:
    registry = registry_with([read_tool()])
    result = PolicyEngine(registry, PolicyRuleSet([])).evaluate("github.get_pr")
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code is ReasonCode.MISSING_POLICY_RULE


def test_policy_unknown_tool_denies() -> None:
    registry = registry_with([read_tool()])
    result = PolicyEngine(registry, allow_rules("github.pr.read")).evaluate("github.absent")
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code is ReasonCode.UNKNOWN_TOOL


def test_policy_credential_unavailable_denies() -> None:
    caps = [capability("github.api"), capability("github.read", state=CapabilityState.READY)]
    tool = read_tool(credential_capability_id="github.read")
    registry = registry_with([tool], caps)
    rules = allow_rules("github.pr.read")

    broker_missing = StaticCredentialBroker([])
    missing = PolicyEngine(registry, rules, broker_missing).evaluate("github.get_pr")
    assert missing.decision is PolicyDecision.DENY
    assert missing.reason_code is ReasonCode.CREDENTIAL_CAPABILITY_UNKNOWN

    broker_degraded = StaticCredentialBroker(
        [
            CredentialCapabilityStatus(
                capability_id="github.read",
                provider="github",
                state=CapabilityState.DEGRADED,
            )
        ]
    )
    degraded = PolicyEngine(registry, rules, broker_degraded).evaluate("github.get_pr")
    assert degraded.decision is PolicyDecision.DENY
    assert degraded.reason_code is ReasonCode.CREDENTIAL_CAPABILITY_NOT_READY

    no_broker = PolicyEngine(registry, rules, None).evaluate("github.get_pr")
    assert no_broker.decision is PolicyDecision.DENY
    assert no_broker.reason_code is ReasonCode.CREDENTIAL_CAPABILITY_UNKNOWN


@pytest.mark.parametrize(
    "state",
    [
        CapabilityState.CONFIGURED,
        CapabilityState.AVAILABLE,
        CapabilityState.HEALTHY,
        CapabilityState.DEGRADED,
        CapabilityState.UNAVAILABLE,
        CapabilityState.DENIED,
    ],
)
def test_policy_capability_not_ready_denies(state: CapabilityState) -> None:
    registry = registry_with([read_tool()], [capability(state=state)])
    result = PolicyEngine(registry, allow_rules("github.pr.read")).evaluate("github.get_pr")
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code is ReasonCode.CAPABILITY_NOT_READY


def test_destructive_t4_denied_by_default_even_with_allow_rule() -> None:
    tool = mutation_tool(
        "github.delete_repo",
        tier=SecurityTier.T4,
        mutation=MutationClass.DESTRUCTIVE,
        policy_action="github.repo.delete",
    )
    registry = registry_with([tool])
    result = PolicyEngine(registry, allow_rules("github.repo.delete")).evaluate(
        "github.delete_repo"
    )
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code is ReasonCode.DESTRUCTIVE_DENIED_BY_DEFAULT


@pytest.mark.parametrize(
    "action", ["*", "github.*", "github.pr.*", "github.?r.read", "github.[a-z]"]
)
def test_wildcard_policy_rule_rejected(action: str) -> None:
    with pytest.raises(PolicyValidationError):
        PolicyRule(policy_action=action, decision=PolicyDecision.ALLOW)


def test_duplicate_policy_rule_rejected() -> None:
    with pytest.raises(PolicyValidationError):
        PolicyRuleSet(
            [
                PolicyRule(policy_action="github.pr.read", decision=PolicyDecision.ALLOW),
                PolicyRule(policy_action="github.pr.read", decision=PolicyDecision.DENY),
            ]
        )


def test_policy_evaluation_reason_codes_are_stable_tokens() -> None:
    registry = registry_with([read_tool()])
    evaluation = PolicyEngine(registry, PolicyRuleSet([])).evaluate("github.get_pr")
    payload = evaluation.canonical()
    assert payload["reason_code"] == "MISSING_POLICY_RULE"
    assert set(payload) == {"decision", "policy_action", "reason_code", "tool_id"}


# ---------------------------------------------------------------------------
# 15. projection inclusion/exclusion
# ---------------------------------------------------------------------------


def _projection_fixture() -> tuple[ToolRegistry, PolicyRuleSet, StaticCredentialBroker]:
    caps = [
        capability("github.api", CapabilityState.READY),
        capability("github.slow", CapabilityState.DEGRADED),
        capability("github.read", CapabilityState.READY),
        capability("github.write", CapabilityState.READY),
    ]
    tools = [
        read_tool("github.get_pr", policy_action="github.pr.read"),  # ALLOW
        read_tool("github.get_issue", policy_action="github.issue.read"),  # APPROVAL
        read_tool("github.get_repo", policy_action="github.repo.read"),  # DENY
        read_tool("github.get_checks", policy_action="github.checks.read"),  # missing rule
        read_tool(
            "github.get_search", capability_id="github.slow", policy_action="github.search"
        ),  # unhealthy
        read_tool(
            "github.get_secretive",
            policy_action="github.secretive.read",
            credential_capability_id="github.write",
        ),  # credential not ready
        mutation_tool(
            "github.delete_repo",
            tier=SecurityTier.T4,
            mutation=MutationClass.DESTRUCTIVE,
            policy_action="github.repo.delete",
        ),  # destructive
    ]
    rules = PolicyRuleSet(
        [
            PolicyRule(policy_action="github.pr.read", decision=PolicyDecision.ALLOW),
            PolicyRule(
                policy_action="github.issue.read", decision=PolicyDecision.APPROVAL_REQUIRED
            ),
            PolicyRule(policy_action="github.repo.read", decision=PolicyDecision.DENY),
            PolicyRule(policy_action="github.search", decision=PolicyDecision.ALLOW),
            PolicyRule(policy_action="github.secretive.read", decision=PolicyDecision.ALLOW),
            PolicyRule(policy_action="github.repo.delete", decision=PolicyDecision.ALLOW),
        ]
    )
    broker = StaticCredentialBroker(
        [
            CredentialCapabilityStatus(
                capability_id="github.write",
                provider="github",
                state=CapabilityState.UNAVAILABLE,
            )
        ]
    )
    return ToolRegistry(CapabilityRegistry(caps), tools), rules, broker


def test_projection_includes_only_allow_and_approval_required() -> None:
    registry, rules, broker = _projection_fixture()
    result = project_capabilities(registry, rules, broker, ProjectionContext())

    assert result.tool_ids == ["github.get_issue", "github.get_pr"]
    by_id = {tool.tool_id: tool for tool in result.tools}
    assert by_id["github.get_pr"].requires_approval is False
    assert by_id["github.get_issue"].requires_approval is True

    excluded = {item.tool_id: item.reason_code for item in result.excluded}
    assert excluded["github.get_repo"] is ReasonCode.EXPLICIT_DENY
    assert excluded["github.get_checks"] is ReasonCode.MISSING_POLICY_RULE
    assert excluded["github.get_search"] is ReasonCode.CAPABILITY_NOT_READY
    assert excluded["github.get_secretive"] is ReasonCode.CREDENTIAL_CAPABILITY_NOT_READY
    assert excluded["github.delete_repo"] is ReasonCode.DESTRUCTIVE_DENIED_BY_DEFAULT


def test_projection_is_deterministic_and_ordered() -> None:
    registry, rules, broker = _projection_fixture()
    first = project_capabilities(registry, rules, broker)
    second = project_capabilities(registry, rules, broker)
    assert first.tool_ids == sorted(first.tool_ids)
    assert first.projection_hash() == second.projection_hash()
    assert len(first.projection_hash()) == 64


def test_projection_carries_capability_snapshot_hash() -> None:
    registry, rules, broker = _projection_fixture()
    result = project_capabilities(registry, rules, broker)
    assert result.capability_snapshot_hash == registry.capability_snapshot_hash()


def test_unregistered_terminal_and_filesystem_ids_are_not_projected() -> None:
    """Phase 1 registers no generic terminal/filesystem tool, so none appears.

    This asserts the actual guarantee — only explicitly registered canonical
    tools are projected — rather than a hard-coded provider ban.
    """
    registry, rules, broker = _projection_fixture()
    result = project_capabilities(registry, rules, broker)

    for tool_id in ("terminal.exec", "filesystem.any"):
        assert tool_id not in result.tool_ids
    assert not any(
        tool.provider in ("terminal", "filesystem", "shell") for tool in result.tools
    )
    # every projected tool is one that was explicitly registered
    assert set(result.tool_ids) <= {tool.tool_id for tool in registry.ordered()}


@pytest.mark.parametrize("tool_id", ["terminal.exec", "filesystem.any", "shell.run"])
def test_unknown_tool_ids_are_denied(tool_id: str) -> None:
    registry, rules, broker = _projection_fixture()
    evaluation = PolicyEngine(registry, rules, broker).evaluate(tool_id)
    assert evaluation.decision is PolicyDecision.DENY
    assert evaluation.reason_code is ReasonCode.UNKNOWN_TOOL
    assert evaluation.allowed is False


def test_projection_accepts_a_typed_credential_broker() -> None:
    """The contract is ``CredentialBroker | None``, not ``Any``."""
    registry, rules, broker = _projection_fixture()
    assert isinstance(broker, CredentialBroker)
    assert project_capabilities(registry, rules, None) is not None
    assert project_capabilities(registry, rules, broker) is not None


def test_projection_context_is_opaque_and_not_serialized() -> None:
    registry, rules, broker = _projection_fixture()
    context = ProjectionContext(
        principal_ref="opaque-principal", resource_scope_ref="opaque-scope"
    )
    with_ctx = project_capabilities(registry, rules, broker, context)
    without_ctx = project_capabilities(registry, rules, broker)
    assert with_ctx.projection_hash() == without_ctx.projection_hash()
    payload = canonical_json_text(with_ctx.canonical())
    assert "opaque-principal" not in payload
    assert "opaque-scope" not in payload


def test_projection_payload_field_allowlist() -> None:
    registry, rules, broker = _projection_fixture()
    result = project_capabilities(registry, rules, broker)
    allowed = {
        "description",
        "execution_mode",
        "input_schema",
        "mutation_class",
        "operation",
        "output_schema",
        "provider",
        "read_only",
        "requires_approval",
        "result_shaping",
        "security_tier",
        "timeout_seconds",
        "tool_id",
        "version",
    }
    for tool in result.canonical()["tools"]:
        assert set(tool) == allowed
        assert "credential_capability_id" not in tool
        assert "backend" not in tool
        assert "capability_id" not in tool


# ---------------------------------------------------------------------------
# 16/17. secret absence
# ---------------------------------------------------------------------------


def test_projection_and_snapshot_never_contain_secret_sentinels() -> None:
    caps = [capability("github.api"), capability("github.read")]
    tool = read_tool(credential_capability_id="github.read")
    registry = ToolRegistry(CapabilityRegistry(caps), [tool])
    broker = StaticCredentialBroker(
        [
            CredentialCapabilityStatus(
                capability_id="github.read", provider="github", state=CapabilityState.READY
            )
        ]
    )
    projection = project_capabilities(registry, allow_rules("github.pr.read"), broker)

    blobs = [
        registry.snapshot().canonical_json,
        canonical_json_text(projection.canonical()),
        canonical_json_text([status.canonical() for status in broker.ordered()]),
    ]
    for blob in blobs:
        assert SECRET_SENTINEL not in blob
        assert SECRET_PATH_SENTINEL not in blob
        lowered = blob.lower()
        for token in ("bearer ", "password", "private_key", "ghp_", "cookie"):
            assert token not in lowered


def test_credential_status_cannot_carry_secret_material() -> None:
    with pytest.raises(RegistryValidationError):
        CredentialCapabilityStatus(
            capability_id="github.read",
            provider="github",
            state=CapabilityState.READY,
            token=SECRET_SENTINEL,
        )
    status = CredentialCapabilityStatus(
        capability_id="github.read", provider="github", state=CapabilityState.READY
    )
    payload = status.canonical()
    assert set(payload) == {"capability_id", "provider", "state", "version"}
    assert SECRET_SENTINEL not in canonical_json_text(payload)


def test_credential_broker_interface_exposes_status_only() -> None:
    broker = StaticCredentialBroker(
        [
            CredentialCapabilityStatus(
                capability_id="github.read", provider="github", state=CapabilityState.READY
            )
        ]
    )
    assert isinstance(broker, CredentialBroker)
    public = {name for name in dir(broker) if not name.startswith("_")}
    assert public == {"is_ready", "ordered", "status"}
    for forbidden in ("secret", "token", "material", "value", "path", "reveal"):
        assert not any(forbidden in name for name in public)
    assert broker.is_ready("github.read") is True
    assert broker.is_ready("github.absent") is False
    assert broker.status("github.absent") is None


def test_tool_definition_has_no_field_able_to_carry_a_secret() -> None:
    """The guarantee is structural, not heuristic.

    There is no field for a raw credential value, a secret path or an env var
    name, and ``extra`` is forbidden, so one cannot be smuggled in.
    """
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "credential_secret": SECRET_SENTINEL})
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "credential_path": SECRET_PATH_SENTINEL})
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "env": {"TOKEN": SECRET_SENTINEL}})

    fields = set(ToolDefinition.model_fields)
    for forbidden in ("secret", "password", "token", "env", "credential_value"):
        assert forbidden not in fields
    # the only credential reference is an opaque id
    assert "credential_capability_id" in fields


def test_legitimate_human_description_is_not_rejected() -> None:
    """Prose is not scanned: no false positives on ordinary wording."""
    for text in (
        "Collect real token accounting from the state DB.",
        "Backwards patch compatibility for older payloads.",
        "Reads the repository, no authorization changes.",
    ):
        tool = ToolDefinition(**{**read_tool().model_dump(), "description": text})
        assert tool.description == text


def test_backend_identifier_segment_matching_is_exact() -> None:
    """``backend`` rejects by exact segment, never by substring.

    ``patch-compatibility`` contains the substring ``pat`` and
    ``authorization`` is a whole word inside no segment here — neither has a
    segment equal to a credential name, so both are accepted. A backend whose
    own segment *is* ``token``/``password``/... is rejected, which for a
    structured identifier is the intended behaviour (prose is handled by
    ``description``, which is not scanned at all).
    """
    for accepted in ("github-api", "patch-compatibility", "gitlab.rest-v4", "pat-service"):
        tool = ToolDefinition(**{**read_tool().model_dump(), "backend": accepted})
        assert tool.backend == accepted

    for rejected in ("vault.token", "store.password", "api_key", "svc-secret"):
        with pytest.raises(RegistryValidationError):
            ToolDefinition(**{**read_tool().model_dump(), "backend": rejected})


def test_resource_key_selector_segment_matching_is_exact() -> None:
    assert ResourceKey(scope="repository", selector="patch-compatibility").selector
    with pytest.raises(RegistryValidationError):
        ResourceKey(scope="repository", selector="vault.token")


def test_schema_may_declare_a_sensitive_property_without_a_value() -> None:
    """Naming a runtime field ``token`` is legitimate and must be allowed."""
    schema = {
        "type": "object",
        "properties": {
            "token": {"type": "string", "description": "Runtime bearer token."},
            "count": {"type": "integer", "default": 5},
        },
    }
    tool = ToolDefinition(**{**read_tool().model_dump(), "input_schema": schema})
    assert "token" in tool.input_schema["properties"]


@pytest.mark.parametrize("keyword", ["default", "const", "example", "examples"])
def test_schema_materializing_a_credential_is_rejected(keyword: str) -> None:
    value = [SECRET_SENTINEL] if keyword == "examples" else SECRET_SENTINEL
    schema = {
        "type": "object",
        "properties": {"token": {"type": "string", keyword: value}},
    }
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "input_schema": schema})
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "output_schema": schema})


def test_schema_empty_materialized_value_on_sensitive_property_is_allowed() -> None:
    schema = {
        "type": "object",
        "properties": {"password": {"type": "string", "default": ""}},
    }
    tool = ToolDefinition(**{**read_tool().model_dump(), "input_schema": schema})
    assert tool.input_schema["properties"]["password"]["default"] == ""


@pytest.mark.parametrize(
    "schema",
    [
        # nested under $defs
        {
            "type": "object",
            "properties": {"auth": {"$ref": "#/$defs/Auth"}},
            "$defs": {
                "Auth": {
                    "type": "object",
                    "properties": {"api_key": {"type": "string", "const": SECRET_SENTINEL}},
                }
            },
        },
        # nested under items
        {
            "type": "object",
            "properties": {
                "creds": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "private_key": {"type": "string", "default": SECRET_SENTINEL}
                        },
                    },
                }
            },
        },
        # nested under a combinator
        {
            "type": "object",
            "properties": {
                "auth": {
                    "anyOf": [
                        {"type": "null"},
                        {
                            "type": "object",
                            "properties": {
                                "authorization": {
                                    "type": "string",
                                    "example": SECRET_SENTINEL,
                                }
                            },
                        },
                    ]
                }
            },
        },
        # deeply nested property
        {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {
                        "inner": {
                            "type": "object",
                            "properties": {
                                "cookie": {"type": "string", "default": SECRET_SENTINEL}
                            },
                        }
                    },
                }
            },
        },
    ],
)
def test_nested_materialized_credentials_are_rejected(schema: dict[str, Any]) -> None:
    with pytest.raises(RegistryValidationError):
        ToolDefinition(**{**read_tool().model_dump(), "input_schema": schema})


def test_sentinel_never_appears_in_snapshot_or_projection() -> None:
    """A sentinel placed in every place it is still legal must not leak."""
    caps = CapabilityRegistry([capability("github.api")])
    registry = ToolRegistry(caps)
    # legal placements: prose description, and a declared (valueless) property
    tool = ToolDefinition(
        **{
            **read_tool().model_dump(),
            "description": f"Notes {SECRET_SENTINEL}",
            "input_schema": {
                "type": "object",
                "properties": {"token": {"type": "string"}},
            },
        }
    )
    registry.register(tool)
    rules = PolicyRuleSet(
        [
            PolicyRule(
                policy_action="github.pr.read",
                decision=PolicyDecision.ALLOW,
                note=f"operator note {SECRET_SENTINEL}",
            )
        ]
    )

    # the rule note must never reach a canonical form
    assert SECRET_SENTINEL not in canonical_json_text(rules.canonical())
    assert "note" not in rules.canonical()[0]

    snapshot = registry.capability_snapshot_hash()
    assert SECRET_SENTINEL not in snapshot

    result = project_capabilities(registry, rules)
    payload = canonical_json_text(result.canonical())
    assert SECRET_PATH_SENTINEL not in payload
    assert SECRET_SENTINEL not in canonical_json_text(
        [evaluation.canonical() for evaluation in result.excluded]
    )


# ---------------------------------------------------------------------------
# 18. V1 isolation
# ---------------------------------------------------------------------------


def test_v1_tool_surface_is_still_exactly_27() -> None:
    required = contracts.required_tools(contracts.CURRENT_CONTRACT_VERSION)
    assert len(required) == 27
    assert contracts.expected_tool_count(contracts.CURRENT_CONTRACT_VERSION) == 27
    assert contracts.SCHEMA_VERSION == "0.6.1"


def test_v2_package_is_not_imported_by_v1_modules() -> None:
    import pathlib

    root = pathlib.Path(contracts.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "/v2/" in path.as_posix() or path.parent.name == "v2":
            continue
        text = path.read_text(encoding="utf-8")
        if "hermes_mcp_bridge.v2" in text or "from .v2" in text or "import v2" in text:
            offenders.append(path.name)
    assert offenders == []


def test_importing_v2_does_not_mutate_v1_contract() -> None:
    before = set(contracts.required_tools(contracts.CURRENT_CONTRACT_VERSION))
    import hermes_mcp_bridge.v2 as v2_package

    assert v2_package.PHASE1_STATUS == "PHASE_1_CORE_IMPLEMENTED_NOT_ACCEPTED"
    after = set(contracts.required_tools(contracts.CURRENT_CONTRACT_VERSION))
    assert before == after
    assert len(after) == 27
