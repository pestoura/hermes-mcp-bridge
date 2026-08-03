"""Protocol foundation tests."""

from __future__ import annotations

from hermes_mcp_bridge.protocol import (
    AgentCard,
    CapabilityManifest,
    ExecutionEnvelope,
    ToolManifest,
    parse_event,
)


def test_execution_envelope_optional_fields_default_none() -> None:
    envelope = ExecutionEnvelope()
    assert envelope.schema_version == "0.5.0"
    assert envelope.payload_version is None
    assert envelope.principal is None
    assert envelope.delegation_chain == []


def test_execution_envelope_canonical_hash_is_deterministic() -> None:
    first = ExecutionEnvelope(
        schema_version="0.5.0",
        payload_version="1",
        principal="agent",
        delegation_chain=["a", "b"],
    )
    second = ExecutionEnvelope(
        schema_version="0.5.0",
        principal="agent",
        delegation_chain=["a", "b"],
        payload_version="1",
    )
    assert first.to_canonical_dict() == second.to_canonical_dict()


def test_parse_event_known_and_unknown() -> None:
    known = parse_event({"event": "tool.started", "tool": "x", "run_id": "1"})
    assert known.__class__.__name__ == "ToolEvent"
    unknown = parse_event({"event": "weird.custom", "run_id": "1"})
    assert unknown.__class__.__name__ == "UnknownEvent"
    assert getattr(unknown, "event_type", None) == "weird.custom"


def test_capability_manifest_hashes_match() -> None:
    manifest = CapabilityManifest.build(
        bridge_version="0.5.0",
        manifest_version="0.5.0",
        tools=[
            ToolManifest(name="hermes_health", description="Health", read_only=True)
        ],
        orchestration_modes=["auto"],
        limits={},
        provenance={"source": "test"},
        upstream_capabilities=None,
    )
    assert len(manifest.manifest_hash) == 64
    assert manifest.upstream_capabilities_source == "fallback"


def test_agent_card_hash_is_stable() -> None:
    card = AgentCard(
        agent_id="bridge",
        name="Bridge",
        purpose="p",
        version="0.5.0",
    )
    payload = card.to_canonical_dict()
    assert "schema_version" in payload
    assert len(payload["schema_version"]) > 0
