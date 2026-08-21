from hermes_mcp_bridge.v2.enums import (
    ApprovalRequirement,
    MutationClass,
    SecurityTier,
)
from hermes_mcp_bridge.v2.factory_registry import (
    FACTORY_NORTHBOUND_TOOL_IDS,
    build_factory_northbound_registry,
    factory_northbound_definitions,
    factory_northbound_policy_rules,
)
from hermes_mcp_bridge.v2.projection import project_capabilities


def test_factory_northbound_has_closed_typed_tool_surface() -> None:
    assert FACTORY_NORTHBOUND_TOOL_IDS == (
        "factory.acceptance",
        "factory.evidence",
        "factory.protected_mutation_intent",
        "factory.status",
    )


def test_factory_northbound_registry_is_read_first_and_fail_closed() -> None:
    definitions = factory_northbound_definitions()
    assert tuple(tool.tool_id for tool in definitions) == FACTORY_NORTHBOUND_TOOL_IDS

    by_id = {tool.tool_id: tool for tool in definitions}
    for tool_id in ("factory.acceptance", "factory.evidence", "factory.status"):
        tool = by_id[tool_id]
        assert tool.provider == "factory"
        assert tool.backend == "factory-control"
        assert tool.read_only is True
        assert tool.security_tier is SecurityTier.T1
        assert tool.mutation_class is MutationClass.NONE
        assert tool.credential_capability_id is None
        assert tool.input_schema["required"] == ["candidate_sha", "principal"]

    mutation = by_id["factory.protected_mutation_intent"]
    assert mutation.read_only is False
    assert mutation.security_tier is SecurityTier.T3
    assert mutation.mutation_class is MutationClass.PRIVILEGED
    assert mutation.approval_requirement is ApprovalRequirement.REQUIRED
    assert mutation.credential_capability_id is None
    assert mutation.input_schema["properties"]["action"]["enum"] == [
        "ACTIVATE_PROFILE",
        "ACTIVATE_SKILL",
        "MERGE_PR",
        "RELEASE",
    ]


def test_factory_northbound_projection_never_presents_mutation_as_unapproved() -> None:
    projection = project_capabilities(
        build_factory_northbound_registry(),
        factory_northbound_policy_rules(),
    )
    projected = {tool.tool_id: tool for tool in projection.tools}

    assert set(projected) == set(FACTORY_NORTHBOUND_TOOL_IDS)
    assert projected["factory.status"].requires_approval is False
    assert projected["factory.evidence"].requires_approval is False
    assert projected["factory.acceptance"].requires_approval is False
    assert projected["factory.protected_mutation_intent"].requires_approval is True
    assert all(tool.execution_mode.value == "DIRECT" for tool in projected.values())
