"""Packaging contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml

REQUIRED_DOCKERFILE_TOKENS = (
    "FROM python:3.11-slim-bookworm",
    "USER bridge:bridge",
    "CMD [\"python\", \"-m\", \"hermes_mcp_bridge.server\"]",
)
COMPOSE_REQUIRED_KEYS = ("user", "volumes", "healthcheck")


def test_version_aligned() -> None:
    init = Path("src/hermes_mcp_bridge/__init__.py").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "__version__ = \"0.3.0\"" in init
    assert "version = \"0.3.0\"" in pyproject


def test_dockerfile_multi_stage_and_nonroot() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.count("FROM") == 2
    assert "USER bridge:bridge" in dockerfile
    for token in REQUIRED_DOCKERFILE_TOKENS:
        assert token in dockerfile


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


def test_smoke_expected_tools_exact() -> None:
    script = Path("scripts/smoke_test.py").read_text(encoding="utf-8")
    assert "EXPECTED_TOOLS = {" in script
    expected = {
        '"hermes_prompt"',
        '"hermes_submit"',
        '"hermes_wait"',
        '"hermes_status"',
        '"hermes_stop"',
        '"hermes_health"',
        '"hermes_recent_runs"',
    }
    for tool in expected:
        assert tool in script
