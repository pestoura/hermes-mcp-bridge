"""Base-image contract tests retained from the 0.9.0 security release.

The 1.0.0 contract baseline deliberately keeps the same hardened runtime:

* ``python:3.12-slim-trixie`` remains pinned by digest;
* the tool surface remains at 27 tools;
* the wire schema remains ``0.6.1``.

These tests are static so they run on the host without Docker. They remain
strict about the digest pin because a floating tag would reintroduce CVE drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from hermes_mcp_bridge import __version__
from hermes_mcp_bridge.contracts import (
    CURRENT_CONTRACT_VERSION,
    SCHEMA_VERSION,
    expected_tool_count,
    required_tools,
)

DOCKERFILE = Path("Dockerfile")
COMPOSE = Path("compose.yml")

BASE_IMAGE_DIGEST = "sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
BASE_IMAGE_REF = f"python:3.12-slim-trixie@{BASE_IMAGE_DIGEST}"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _from_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.startswith("FROM ")]


# --------------------------------------------------------------------------
# Base image pinning
# --------------------------------------------------------------------------


def test_base_image_arg_is_pinned_by_digest() -> None:
    text = _dockerfile()
    assert f"ARG BASE_IMAGE={BASE_IMAGE_REF}" in text
    assert _DIGEST_RE.fullmatch(BASE_IMAGE_DIGEST)


def _instructions(text: str) -> str:
    """Return the Dockerfile with comment lines stripped."""

    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def test_base_image_is_python_312_slim_trixie() -> None:
    instructions = _instructions(_dockerfile())
    assert "python:3.12-slim-trixie@" in instructions
    assert "3.11-slim-bookworm" not in instructions
    assert "3.13-slim" not in instructions


def test_both_stages_use_the_same_pinned_arg() -> None:
    """A second literal FROM would let the stages drift apart."""

    froms = _from_lines(_dockerfile())
    assert froms == [
        "FROM ${BASE_IMAGE} AS builder",
        "FROM ${BASE_IMAGE} AS runtime",
    ]


def test_no_floating_base_tag_anywhere_in_dockerfile() -> None:
    """Every ``python:`` reference must carry an ``@sha256:`` digest."""

    for line in _dockerfile().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "python:" in stripped:
            assert "@sha256:" in stripped, f"unpinned base reference: {stripped}"


# --------------------------------------------------------------------------
# Hardening: non-root, no systemd, apt hygiene
# --------------------------------------------------------------------------


def test_runtime_runs_as_non_root_bridge_user() -> None:
    text = _dockerfile()
    assert "useradd -u 1000 -g 1000" in text
    assert "groupadd -g 1000 bridge" in text
    assert "USER bridge:bridge" in text
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    user_index = lines.index("USER bridge:bridge")
    assert lines[-1].startswith('CMD ["python"')
    assert user_index < len(lines) - 1


def _installed_packages(text: str) -> str:
    """Return only the lowercased text of the apt-get install lines."""

    return " ".join(line.lower() for line in text.splitlines() if "apt-get install" in line)


def test_systemd_is_not_installed_in_the_image() -> None:
    installed = _installed_packages(_dockerfile())
    for token in ("systemd", "systemctl", "dbus", "init-system-helpers"):
        assert token not in installed, f"unexpected package in image: {token}"


def test_apt_installs_are_minimal_and_cleaned() -> None:
    text = _dockerfile()
    install_lines = [line for line in text.splitlines() if "apt-get install" in line]
    assert install_lines, "expected at least one apt-get install line"
    for line in install_lines:
        assert "--no-install-recommends" in line
    assert text.count("rm -rf /var/lib/apt/lists/*") == len(install_lines)
    assert "apt-get clean" in text


def test_runtime_stage_installs_ca_certificates_for_tls() -> None:
    text = _dockerfile()
    runtime = text.split("FROM ${BASE_IMAGE} AS runtime", 1)[1]
    assert "ca-certificates" in runtime
    assert "update-ca-certificates" in runtime


def test_no_build_toolchain_in_the_runtime_stage() -> None:
    text = _dockerfile()
    builder, runtime = text.split("FROM ${BASE_IMAGE} AS runtime", 1)
    assert "build-essential" in builder
    assert "build-essential" not in runtime
    assert "--no-index" in runtime, "runtime must install only prebuilt wheels"


def test_state_directory_is_owned_by_the_bridge_user() -> None:
    text = _dockerfile()
    assert "mkdir -p /var/lib/hermes-mcp-bridge" in text
    assert "chown -R bridge:bridge /var/lib/hermes-mcp-bridge" in text


def test_pycache_and_pip_cache_are_removed() -> None:
    text = _dockerfile()
    assert "--no-cache-dir" in text
    assert "__pycache__" in text
    assert "rm -rf /root/.cache" in text


# --------------------------------------------------------------------------
# SQLite / compose runtime expectations
# --------------------------------------------------------------------------


def test_sqlite_state_path_is_available_to_the_runtime() -> None:
    from hermes_mcp_bridge.config import Settings

    default = Settings.model_fields["bridge_state_db_path"].default
    assert default == "/var/lib/hermes-mcp-bridge/state.sqlite3"
    assert "mkdir -p /var/lib/hermes-mcp-bridge" in _dockerfile()


def test_compose_still_mounts_state_and_drops_privileges() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["hermes-mcp-bridge"]
    assert service["user"] == "${BRIDGE_UID:-1000}:${BRIDGE_GID:-1000}"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert any(":/var/lib/hermes-mcp-bridge" in item for item in service["volumes"])
    assert "python -m hermes_mcp_bridge.healthcheck" in service["healthcheck"]["test"][-1]


# --------------------------------------------------------------------------
# Version / contract alignment for 1.0.0
# --------------------------------------------------------------------------


def test_version_is_1_0_0_everywhere() -> None:
    assert __version__ == "1.0.0"
    assert CURRENT_CONTRACT_VERSION == "1.0.0"
    init = Path("src/hermes_mcp_bridge/__init__.py").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    config = Path("src/hermes_mcp_bridge/config.py").read_text(encoding="utf-8")
    assert '__version__ = "1.0.0"' in init
    assert 'version = "1.0.0"' in pyproject
    assert 'bridge_version: str = "1.0.0"' in config


def test_schema_version_is_still_0_6_1() -> None:
    assert SCHEMA_VERSION == "0.6.1"


def test_tool_contract_is_unchanged_at_27_tools() -> None:
    tools = required_tools(CURRENT_CONTRACT_VERSION)
    assert expected_tool_count(CURRENT_CONTRACT_VERSION) == 27
    assert "hermes_readiness" in tools
    assert tools == required_tools("0.9.0")


def test_python_floor_supports_the_runtime_base() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.11"' in pyproject
