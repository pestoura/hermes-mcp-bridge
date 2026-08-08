from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

from hermes_mcp_bridge.v2.shadow_isolation import (
    SHADOW_HERMES_TOOL_NAMES,
    SHADOW_HTTP_METHODS,
    SHADOW_ISOLATION_SCHEMA,
    SHADOW_MCP_SERVER,
    SHADOW_SERVER_CONTRACT,
    validate_shadow_isolation,
)

ROOT = Path(__file__).resolve().parents[1]
STRICT_VALIDATOR = ROOT / "scripts" / "validate_v2_phase2_connected_gate.py"
CONNECTED_TEST = ROOT / "tests" / "test_v2_phase2_connected_acceptance.py"
SERVER = ROOT / "scripts" / "v2_phase2_shadow_github_mcp.py"
PREPARE = ROOT / "scripts" / "v2_phase2_prepare_shadow_home.py"
PROBE = ROOT / "scripts" / "v2_phase2_probe_shadow_runtime.py"
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
        "effective_toolsets": [SHADOW_MCP_SERVER],
        "native_toolsets_enabled": [],
        "effective_tools": sorted(SHADOW_HERMES_TOOL_NAMES),
        "resolver_exact": True,
        "mcp_server_config_exact": True,
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


def test_shadow_isolation_uses_current_hermes_mcp_naming() -> None:
    assert SHADOW_MCP_SERVER == "phase2-read"
    assert sorted(SHADOW_HERMES_TOOL_NAMES) == sorted(
        {
            "mcp__phase2_read__github_get_checks",
            "mcp__phase2_read__github_get_issue",
            "mcp__phase2_read__github_get_pr",
            "mcp__phase2_read__github_get_repo",
            "mcp__phase2_read__github_search",
        }
    )


def test_shadow_isolation_rejects_extra_tool() -> None:
    payload = valid_shadow()
    payload["effective_tools"].append("terminal")
    assert "shadow_isolation_tools_not_exact" in validate_shadow_isolation(
        payload, repositories={REPOSITORY}, source_commit=SOURCE_COMMIT
    )


def test_shadow_isolation_rejects_native_toolset() -> None:
    payload = valid_shadow()
    payload["native_toolsets_enabled"] = ["terminal"]
    assert "shadow_isolation_native_toolsets_not_empty" in validate_shadow_isolation(
        payload, repositories={REPOSITORY}, source_commit=SOURCE_COMMIT
    )


def test_shadow_isolation_rejects_unproven_resolver_or_config() -> None:
    for field in ("resolver_exact", "mcp_server_config_exact"):
        payload = valid_shadow()
        payload[field] = False
        assert f"shadow_isolation_invalid:{field}" in validate_shadow_isolation(
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


def test_shadow_home_config_uses_exact_mcp_server_allowlist() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    assert 'target["platform_toolsets"] = {"api_server": [SHADOW_MCP_SERVER]}' in text
    assert "_ensure_hermes_runtime_python(args, raw_argv)" in text
    assert "resolve_hermes_python(" in text
    assert "_constrain_platform_to_shadow_mcp(target)" in text
    assert 'final != {SHADOW_MCP_SERVER}' in text
    assert '"resources": False' in text
    assert '"prompts": False' in text
    assert '"supports_parallel_tool_calls": False' in text
    assert '"messaging_credentials_copied": False' in text
    assert '"integration_credentials_copied": False' in text
    assert 'source.get("fallback_providers")' not in text


def test_shadow_probe_composes_native_endpoint_and_actual_resolver_proof() -> None:
    text = PROBE.read_text(encoding="utf-8")
    assert 'if native_enabled:' in text
    assert 'ProbeError("SHADOW_NATIVE_TOOLSETS_NOT_EMPTY")' in text
    assert "_validated_hermes_runtime_python(args.hermes_python, shadow_home)" in text
    assert "validate_hermes_python_hint(" in text
    assert '_get_platform_tools(config, "api_server")' in text
    assert 'resolved != [SHADOW_MCP_SERVER]' in text
    assert 'ProbeError("SHADOW_EFFECTIVE_TOOLSETS_NOT_EXACT")' in text
    assert 'server_cfg["tools"].get("include") == expected_tools' in text
    assert 'server_cfg["tools"].get("resources") is False' in text
    assert 'server_cfg["tools"].get("prompts") is False' in text
