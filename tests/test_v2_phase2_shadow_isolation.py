from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

from hermes_mcp_bridge.v2.shadow_isolation import (
    SHADOW_HERMES_TOOL_NAMES,
    SHADOW_HTTP_METHODS,
    SHADOW_ISOLATION_SCHEMA,
    SHADOW_SERVER_CONTRACT,
    SHADOW_TOOLSET,
    validate_shadow_isolation,
)

ROOT = Path(__file__).resolve().parents[1]
STRICT_VALIDATOR = ROOT / "scripts" / "validate_v2_phase2_connected_gate.py"
CONNECTED_TEST = ROOT / "tests" / "test_v2_phase2_connected_acceptance.py"
SERVER = ROOT / "scripts" / "v2_phase2_shadow_github_mcp.py"
PREPARE = ROOT / "scripts" / "v2_phase2_prepare_shadow_home.py"
REPOSITORY = "pestoura/hermes-mcp-bridge"
SOURCE_COMMIT = "1" * 40


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_shadow() -> dict[str, Any]:
    return {
        "schema": SHADOW_ISOLATION_SCHEMA,
        "source_commit": SOURCE_COMMIT,
        "connected_jarvas": True,
        "hermes_profile_isolated": True,
        "api_platform": "api_server",
        "api_bind_loopback": True,
        "api_auth_required": True,
        "effective_toolsets": [SHADOW_TOOLSET],
        "effective_tools": sorted(SHADOW_HERMES_TOOL_NAMES),
        "repository_scopes": [REPOSITORY],
        "credential_provider_type": "github_app",
        "credential_capability": "github.read",
        "credential_file_backed": True,
        "mcp_resources_enabled": False,
        "mcp_prompts_enabled": False,
        "http_methods": SHADOW_HTTP_METHODS,
        "generic_execution_tools": False,
        "mutation_capable_tools": False,
        "server_contract": SHADOW_SERVER_CONTRACT,
        "probes": {
            "health_status": 200,
            "capabilities_status": 200,
            "toolsets_status": 200,
            "sessions_status": 200,
        },
        "confirmed_at": "2026-08-08T15:30:00+00:00",
    }


def test_exact_shadow_isolation_contract_is_accepted() -> None:
    assert validate_shadow_isolation(
        valid_shadow(), repositories={REPOSITORY}, source_commit=SOURCE_COMMIT
    ) == []


def test_shadow_isolation_rejects_extra_tool() -> None:
    payload = valid_shadow()
    payload["effective_tools"].append("terminal")
    assert "shadow_isolation_tools_not_exact" in validate_shadow_isolation(
        payload, repositories={REPOSITORY}, source_commit=SOURCE_COMMIT
    )


def test_shadow_isolation_rejects_repo_or_commit_drift() -> None:
    payload = valid_shadow()
    failures = validate_shadow_isolation(
        payload,
        repositories={"pestoura/unexpected"},
        source_commit="2" * 40,
    )
    assert "shadow_isolation_source_commit_invalid" in failures
    assert "shadow_isolation_repository_scopes_not_exact" in failures


def test_strict_gate_requires_mechanical_shadow_proof() -> None:
    connected_module = _load(CONNECTED_TEST, "connected_fixture")
    strict = _load(STRICT_VALIDATOR, "strict_validator")
    connected = connected_module.valid_evidence()
    shadow = valid_shadow()
    assert strict.validate_gate(connected, shadow) == []

    forged = copy.deepcopy(shadow)
    forged["mutation_capable_tools"] = True
    assert "shadow_isolation_invalid:mutation_capable_tools" in strict.validate_gate(
        connected, forged
    )


def test_strict_gate_refuses_audit_basis_without_isolation() -> None:
    connected_module = _load(CONNECTED_TEST, "connected_fixture_audit")
    strict = _load(STRICT_VALIDATOR, "strict_validator_audit")
    connected = connected_module.valid_evidence()
    connected["window_integrity_basis"]["shadow_mutation_basis"] = "github_audit_log_reviewed"
    for sample in connected["samples"]:
        sample["v1_shadow"]["mutation_basis"] = "github_audit_log_reviewed"
    failures = strict.validate_gate(connected, valid_shadow())
    assert "shadow_isolation_basis_not_enforced" in failures


def test_shadow_mcp_surface_has_no_generic_or_mutating_tools() -> None:
    text = SERVER.read_text(encoding="utf-8")
    for tool in (
        "github_get_checks",
        "github_get_issue",
        "github_get_pr",
        "github_get_repo",
        "github_search",
    ):
        assert f'@mcp.tool(name="{tool}")' in text
    for forbidden in (
        "@mcp.tool(name=\"terminal\")",
        "@mcp.tool(name=\"write_file\")",
        "@mcp.tool(name=\"execute_code\")",
        "executor.create_",
        "executor.update_",
        "executor.delete_",
        "executor.merge_",
    ):
        assert forbidden not in text


def test_shadow_home_config_is_explicitly_minimal() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    assert 'target["platform_toolsets"] = {"api_server": [SHADOW_TOOLSET]}' in text
    assert '"resources": False' in text
    assert '"prompts": False' in text
    assert '"supports_parallel_tool_calls": False' in text
    assert '"messaging_credentials_copied": False' in text
    assert '"integration_credentials_copied": False' in text
    assert 'source.get("fallback_providers")' not in text
