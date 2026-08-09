"""Packaging and protocol contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from hermes_mcp_bridge import __version__
from hermes_mcp_bridge.protocol import (
    AgentCard,
    CapabilityManifest,
    ExecutionEnvelope,
    ToolManifest,
    parse_event,
)

#: 0.9.0: the base image is pinned by digest through a single ARG, so both
#: stages are guaranteed to use the very same immutable image.
BASE_IMAGE_DIGEST = "sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
BASE_IMAGE_REF = f"python:3.12-slim-trixie@{BASE_IMAGE_DIGEST}"
REQUIRED_DOCKERFILE_TOKENS = (
    f"ARG BASE_IMAGE={BASE_IMAGE_REF}",
    "FROM ${BASE_IMAGE} AS builder",
    "FROM ${BASE_IMAGE} AS runtime",
    "USER bridge:bridge",
    'CMD ["python", "-m", "hermes_mcp_bridge.http_runner"]',
)
COMPOSE_REQUIRED_KEYS = ("user", "volumes", "healthcheck")


def test_version_aligned() -> None:
    init = Path("src/hermes_mcp_bridge/__init__.py").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert f'__version__ = "{__version__}"' in init
    assert f'version = "{__version__}"' in pyproject


def test_dockerfile_multi_stage_and_nonroot() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2
    assert "USER bridge:bridge" in dockerfile
    for token in REQUIRED_DOCKERFILE_TOKENS:
        assert token in dockerfile


def test_http_runner_preserves_structured_logging() -> None:
    runner = Path("src/hermes_mcp_bridge/http_runner.py").read_text(encoding="utf-8")
    assert "configure_logging(force=True)" in runner
    assert "log_config=None" in runner
    assert "access_log=False" in runner


def test_compose_user_volume_healthcheck_hardening() -> None:
    compose = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["hermes-mcp-bridge"]
    for key in COMPOSE_REQUIRED_KEYS:
        assert key in service, f"missing compose key: {key}"
    assert service["network_mode"] == "host"
    assert "user" in service and service["user"] == "${BRIDGE_UID:-1000}:${BRIDGE_GID:-1000}"
    volume = service.get("volumes", [])
    assert any(":/var/lib/hermes-mcp-bridge" in item for item in volume)
    healthcheck = service.get("healthcheck", {})
    assert "python -m hermes_mcp_bridge.healthcheck" in healthcheck["test"][-1]


def test_smoke_expected_tools_derives_from_contract() -> None:
    """The smoke script must not hard-code a tool list or a blind count."""

    script = Path("scripts/smoke_test.py").read_text(encoding="utf-8")
    assert "EXPECTED_TOOLS = set(required_tools(CURRENT_CONTRACT_VERSION))" in script
    assert "validate_tools(names, version=CURRENT_CONTRACT_VERSION)" in script
    assert '"hermes_quota_status",' not in script

    from hermes_mcp_bridge.contracts import CURRENT_CONTRACT_VERSION, required_tools

    tools = required_tools(CURRENT_CONTRACT_VERSION)
    assert len(tools) == 27
    assert "hermes_readiness" in tools


def test_execution_envelope_optional_fields_default_none() -> None:
    envelope = ExecutionEnvelope()
    assert envelope.schema_version == "0.6.1"
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
        tools=[ToolManifest(name="hermes_health", description="Health", read_only=True)],
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
