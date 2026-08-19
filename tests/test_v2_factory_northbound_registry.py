from hermes_mcp_bridge.v2.factory_registry import FACTORY_NORTHBOUND_TOOL_IDS


def test_factory_northbound_has_closed_typed_tool_surface() -> None:
    assert FACTORY_NORTHBOUND_TOOL_IDS == (
        "factory.evidence",
        "factory.prepare_mutation",
        "factory.projects",
        "factory.status",
    )
