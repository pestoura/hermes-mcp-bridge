"""Contract tests for the stable 1.0.0 tool surface and schema pinning."""

from __future__ import annotations

import asyncio
import importlib
import types
from pathlib import Path

import pytest

from hermes_mcp_bridge import __version__
from hermes_mcp_bridge.contracts import (
    CURRENT_CONTRACT_VERSION,
    SCHEMA_VERSION,
    TOOL_CONTRACTS,
    UnknownContractVersionError,
    contract_digest,
    contract_snapshot,
    diff_tools,
    expected_tool_count,
    required_tools,
    validate_tools,
)
from hermes_mcp_bridge.protocol import (
    validate_manifest_contract,
    validate_manifest_tools,
)


def _server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> types.ModuleType:
    monkeypatch.setenv("HERMES_API_KEY", "unit-test-key-0123456789")
    monkeypatch.setenv("BRIDGE_STATE_DB_PATH", str(tmp_path / "state.sqlite3"))
    return importlib.import_module("hermes_mcp_bridge.server")


def test_current_contract_is_1_0_0() -> None:
    assert CURRENT_CONTRACT_VERSION == "1.0.0"
    assert __version__ == "1.0.0"
    assert __version__ == CURRENT_CONTRACT_VERSION


def test_schema_version_remains_0_6_1() -> None:
    assert SCHEMA_VERSION == "0.6.1"


def test_contract_0_8_2_has_27_tools_including_readiness() -> None:
    tools = required_tools("0.8.2")
    assert expected_tool_count("0.8.2") == 27
    assert len(tools) == 27
    assert "hermes_readiness" in tools


def test_contract_0_6_1_has_26_tools_without_readiness() -> None:
    tools = required_tools("0.6.1")
    assert len(tools) == 26
    assert "hermes_readiness" not in tools


def test_0_8_2_is_additive_over_0_6_1() -> None:
    assert required_tools("0.6.1") < required_tools("0.8.2")
    assert required_tools("0.8.2") - required_tools("0.6.1") == {"hermes_readiness"}


def test_all_0_8_x_share_the_same_tool_set() -> None:
    assert required_tools("0.8.0") == required_tools("0.8.1")
    assert required_tools("0.8.1") == required_tools("0.8.2")


def test_1_0_0_freezes_the_0_9_0_tool_set() -> None:
    assert required_tools("1.0.0") == required_tools("0.9.0")
    assert expected_tool_count("1.0.0") == 27
    assert "hermes_readiness" in required_tools("1.0.0")


def test_contract_snapshot_is_canonical() -> None:
    snapshot = contract_snapshot("1.0.0")
    assert snapshot["contract_version"] == "1.0.0"
    assert snapshot["schema_version"] == "0.6.1"
    tools = snapshot["tools"]
    assert isinstance(tools, list)
    assert tools == sorted(tools)
    assert len(tools) == 27


def test_contract_digest_is_deterministic_and_versioned() -> None:
    first = contract_digest("1.0.0")
    second = contract_digest("1.0.0")
    assert first == second
    assert len(first) == 64
    assert first != contract_digest("0.9.0")


def test_unknown_contract_version_raises() -> None:
    with pytest.raises(UnknownContractVersionError):
        required_tools("9.9.9")


def test_diff_detects_missing_and_extra_tools() -> None:
    observed = (set(required_tools("1.0.0")) - {"hermes_readiness"}) | {
        "hermes_bogus"
    }
    diff = diff_tools(observed, version="1.0.0")
    assert diff["missing"] == ["hermes_readiness"]
    assert diff["extra"] == ["hermes_bogus"]


def test_validate_tools_reports_not_ok_when_missing() -> None:
    observed = set(required_tools("1.0.0")) - {"hermes_readiness"}
    result = validate_tools(observed, version="1.0.0")
    assert result["ok"] is False
    assert result["count"] == 26
    assert result["expected_count"] == 27
    assert result["missing"] == ["hermes_readiness"]
    assert result["contract_digest"] == contract_digest("1.0.0")


def test_validate_tools_allows_additive_extra_tools() -> None:
    observed = set(required_tools("1.0.0")) | {"hermes_future"}
    result = validate_tools(observed, version="1.0.0")
    assert result["ok"] is True
    assert result["extra"] == ["hermes_future"]


def test_no_blind_26_constant_in_sources() -> None:
    """The 26-tool assumption must not be hard-coded outside the contract map."""

    roots = [Path("src/hermes_mcp_bridge"), Path("scripts"), Path("deploy")]
    offenders: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".sh"} or path.name == "contracts.py":
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "EXPECT_TOOL_COUNT=26" in line.replace(" ", ""):
                    offenders.append(f"{path}:{lineno}")
    assert not offenders, f"hard-coded 26-tool assumption: {offenders}"


def test_server_registers_exactly_the_contract_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    names = server.server_tool_names()
    result = validate_tools(names, version=CURRENT_CONTRACT_VERSION)
    assert result["missing"] == []
    assert result["extra"] == []
    assert result["count"] == 27


def test_manifest_matches_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    manifest = asyncio.run(server._build_capability_manifest())
    assert validate_manifest_tools(manifest) == []
    result = validate_manifest_contract(manifest, version=CURRENT_CONTRACT_VERSION)
    assert result["ok"] is True
    assert result["count"] == 27
    assert manifest.bridge_version == CURRENT_CONTRACT_VERSION
    assert manifest.manifest_version == CURRENT_CONTRACT_VERSION


def test_manifest_preserves_readiness_version_added(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    manifest = asyncio.run(server._build_capability_manifest())
    readiness = next(tool for tool in manifest.tools if tool.name == "hermes_readiness")
    assert readiness.version_added == "0.8.0"


def test_contract_map_is_read_only() -> None:
    with pytest.raises(TypeError):
        TOOL_CONTRACTS["1.0.0"] = frozenset()  # type: ignore[index]
