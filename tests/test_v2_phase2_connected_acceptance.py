"""Fail-closed tests for the Phase 2 connected DIRECT_READ_ACCEPTED gate."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_v2_phase2_direct_read_evidence.py"
TEST_SHA = "1" * 40
TEST_CORE_SHA = "2" * 40
DIGEST = "a" * 64
REPOSITORY = "pestoura/hermes-mcp-bridge"
EXPECTED_TOOLS = (
    "github.get_checks",
    "github.get_issue",
    "github.get_pr",
    "github.get_repo",
    "github.search",
)
DIRECT_MUTATION_BASIS = "executor_http_method_restricted_to_get"
SHADOW_MUTATION_BASIS = "read_only_credential_enforced"
ATTESTATION_SCHEMA = "hermes-v2-phase2-provider-attestation/1"


def _load_validator() -> Any:
    spec = importlib.util.spec_from_file_location("phase2_validator", VALIDATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample(tool_id: str, repetition: int) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "repetition": repetition,
        "repository": REPOSITORY,
        "connected_jarvas": True,
        "contaminated_window": False,
        "window_integrity": {
            "direct_transport_dedicated": True,
            "direct_call_delta_exact": True,
            "shadow_session_scoped_accounting": True,
            "attribution_ambiguity": False,
        },
        "direct": {
            "success": True,
            "latency_ms": 12.5,
            "provider_api_calls": 1,
            "hermes_upstream_calls": 0,
            "hermes_llm_tokens": {"input": 0, "output": 0, "total": 0},
            "raw_bytes": 1200,
            "returned_bytes": 420,
            "mutation_observed": False,
            "mutation_basis": DIRECT_MUTATION_BASIS,
            "redirect_followed": False,
        },
        "v1_shadow": {
            "success": True,
            "latency_ms": 8100.0,
            "hermes_llm_tokens": {"input": 1000, "output": 75, "total": 1075},
            "token_usage_estimated": False,
            "token_usage_source": "hermes_state_db:session_model_usage",
            "mutation_observed": False,
            "mutation_basis": SHADOW_MUTATION_BASIS,
        },
        "comparison": {
            "semantic_match": True,
            "direct_normalized_sha256": DIGEST,
            "v1_normalized_sha256": DIGEST,
        },
    }


def valid_evidence() -> dict[str, Any]:
    samples = [
        _sample(tool_id, repetition) for tool_id in EXPECTED_TOOLS for repetition in range(1, 4)
    ]
    return {
        "schema": "hermes-v2-phase2-direct-read-acceptance/1",
        "gate": "DIRECT_READ_EVIDENCE_COLLECTED",
        "source_commit": TEST_SHA,
        "runtime": {
            "bridge_version": "1.0.0",
            "schema_version": "0.6.1",
            "v1_tool_count": 27,
            "jarvas_connected": True,
            "v1_path_healthy": True,
            "direct_feature_enabled": True,
            "v1_semantics_unchanged": True,
            "direct_core_commit": TEST_CORE_SHA,
        },
        "github_provider": {
            "credential_capability": "github.read",
            "provider_type": "github_app",
            "authenticated": True,
            "least_privilege": True,
            "broad_pat": False,
            "github_api_version": "2026-03-10",
            "base_url": "https://api.github.com",
            "permissions": {
                "checks": "read",
                "issues": "read",
                "metadata": "read",
                "pull_requests": "read",
            },
            "unexpected_permissions": [],
            "repository_scopes": [REPOSITORY],
        },
        "attestation_notes": {
            "attestation_path_recorded": False,
            "permissions_source": "installation_token_mint_response",
            "machine_verified": [
                "authentication",
                "repository_metadata_read",
                "pull_requests_read",
                "issues_read",
                "check_runs_read",
                "installation_repository_set",
            ],
            "externally_confirmed": [
                "exact_permission_map",
                "exact_repository_selection",
            ],
            "declaration": {
                "schema": ATTESTATION_SCHEMA,
                "confirmation": True,
                "confirmation_source": "installation_token_mint_response",
                "confirmed_at": "2026-08-08T10:00:00+00:00",
            },
            "probes": {
                "auth_probe_status": 200,
                "oauth_scopes_header_present": False,
                "repository_probe_count": 1,
                "installation_repository_count": 1,
                "repository_read_probes": {
                    REPOSITORY: {
                        "metadata_status": 200,
                        "pulls_status": 200,
                        "pulls_sample_count": 1,
                        "issues_status": 200,
                        "issues_sample_count": 1,
                        "check_runs_status": 200,
                        "check_runs_total_count": 3,
                    }
                },
            },
        },
        "window_integrity_basis": {
            "direct_mutation_basis": DIRECT_MUTATION_BASIS,
            "shadow_mutation_basis": SHADOW_MUTATION_BASIS,
            "shadow_mutation_basis_description": "documented observational basis",
        },
        "canary": {
            "direct_feature_enabled": True,
            "canary_tool_ids": sorted(EXPECTED_TOOLS),
            "canary_repositories": [REPOSITORY],
            "wildcard_scopes": False,
        },
        "discovery": {
            "actual_jarvas_host": True,
            "credential_source_discovered": True,
            "repository_access_verified": True,
            "credential_value_recorded": False,
            "secret_path_recorded": False,
            "environment_dump_recorded": False,
        },
        "samples": samples,
        "aggregate": {
            "sample_count": 15,
            "successful_samples": 15,
            "semantic_matches": 15,
            "direct_provider_api_calls": 15,
            "direct_hermes_upstream_calls": 0,
            "direct_hermes_llm_tokens": 0,
            "v1_shadow_hermes_llm_tokens": 16125,
            "mutations_observed": 0,
            "contaminated_windows": 0,
            "tool_sample_counts": {tool: 3 for tool in EXPECTED_TOOLS},
        },
        "privacy": {
            "credential_values_stored": False,
            "environment_dump_stored": False,
            "outputs_stored": False,
            "prompts_stored": False,
            "secret_paths_stored": False,
        },
    }


def _validate(payload: dict[str, Any]) -> list[str]:
    return _load_validator().validate_evidence(payload)


def test_complete_connected_contract_is_accepted() -> None:
    assert _validate(valid_evidence()) == []


def test_cli_emits_direct_read_accepted(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    gate = tmp_path / "gate.json"
    evidence.write_text(json.dumps(valid_evidence()), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(evidence), "--json-out", str(gate)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(gate.read_text(encoding="utf-8")) == {
        "failures": [],
        "gate": "DIRECT_READ_ACCEPTED",
        "source_commit": TEST_SHA,
    }


@pytest.mark.parametrize(
    ("mutator", "failure"),
    [
        (
            lambda p: p.update({"schema": "wrong"}),
            "invalid_schema",
        ),
        (
            lambda p: p["runtime"].update({"jarvas_connected": False}),
            "runtime_invalid:jarvas_connected",
        ),
        (
            lambda p: p["runtime"].update({"v1_tool_count": 28}),
            "runtime_invalid:v1_tool_count",
        ),
        (
            lambda p: p["github_provider"].update({"broad_pat": True}),
            "provider_broad_pat_not_rejected",
        ),
        (
            lambda p: p["github_provider"].update({"least_privilege": False}),
            "provider_not_least_privilege",
        ),
        (
            lambda p: p["github_provider"].update(
                {"unexpected_permissions": ["administration:write"]}
            ),
            "provider_has_unexpected_permissions",
        ),
        (
            lambda p: p["discovery"].update({"actual_jarvas_host": False}),
            "discovery_invalid:actual_jarvas_host",
        ),
        (
            lambda p: p["samples"][0]["direct"].update({"hermes_upstream_calls": 1}),
            "sample[0]:direct_hermes_upstream_not_zero",
        ),
        (
            lambda p: p["samples"][0]["direct"].update(
                {"hermes_llm_tokens": {"input": 1, "output": 0, "total": 1}}
            ),
            "sample[0]:direct_llm_tokens_not_zero",
        ),
        (
            lambda p: p["samples"][0]["direct"].update({"provider_api_calls": 2}),
            "sample[0]:direct_api_calls_not_one",
        ),
        (
            lambda p: p["samples"][0]["v1_shadow"].update({"token_usage_estimated": True}),
            "sample[0]:shadow_token_usage_estimated",
        ),
        (
            lambda p: p["samples"][0]["comparison"].update({"semantic_match": False}),
            "sample[0]:semantic_mismatch",
        ),
        (
            lambda p: p["samples"][0]["comparison"].update({"v1_normalized_sha256": "b" * 64}),
            "sample[0]:normalized_digest_mismatch",
        ),
        (
            lambda p: p["samples"][0].update({"repository": "pestoura/not-authorized"}),
            "sample[0]:repository_out_of_scope",
        ),
        (
            lambda p: p["samples"][0]["direct"].update({"mutation_observed": True}),
            "sample[0]:direct_mutation_observed",
        ),
        (
            lambda p: p["samples"][0].update({"contaminated_window": True}),
            "sample[0]:contaminated_window",
        ),
        (
            lambda p: p["privacy"].update({"prompts_stored": True}),
            "privacy_contract_not_met",
        ),
        (
            lambda p: p.update({"token": "forbidden"}),
            "forbidden_evidence_keys:token",
        ),
    ],
)
def test_validator_fails_closed_on_connected_evidence_tampering(
    mutator: Any,
    failure: str,
) -> None:
    payload = copy.deepcopy(valid_evidence())
    mutator(payload)
    assert failure in _validate(payload)


def _drop(payload: dict[str, Any], *path: str) -> None:
    cursor: Any = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor.pop(path[-1], None)


@pytest.mark.parametrize(
    ("mutator", "failure"),
    [
        # ---- attestation: declaration -----------------------------------
        (
            lambda p: _drop(p, "attestation_notes"),
            "attestation_notes_missing",
        ),
        (
            lambda p: p["attestation_notes"].update({"attestation_path_recorded": True}),
            "attestation_path_recorded",
        ),
        (
            lambda p: _drop(p, "attestation_notes", "declaration"),
            "attestation_declaration_missing",
        ),
        (
            lambda p: p["attestation_notes"]["declaration"].update(
                {"schema": "hermes-v2-phase2-provider-attestation/0"}
            ),
            "attestation_declaration_schema_invalid",
        ),
        (
            lambda p: p["attestation_notes"]["declaration"].update({"confirmation": False}),
            "attestation_declaration_not_confirmed",
        ),
        (
            # a mint-response source is impossible for a fine-grained token
            lambda p: (
                p["github_provider"].update({"provider_type": "fine_grained_token"}),
                p["attestation_notes"]["declaration"].update(
                    {"confirmation_source": "installation_token_mint_response"}
                ),
            ),
            "attestation_confirmation_source_not_allowed",
        ),
        (
            lambda p: p["attestation_notes"]["declaration"].update(
                {"confirmed_at": "2026-08-08T10:00:00"}
            ),
            "attestation_confirmed_at_not_timezone_aware",
        ),
        (
            lambda p: p["attestation_notes"].update(
                {"externally_confirmed": ["exact_permission_map"]}
            ),
            "attestation_externally_confirmed_invalid",
        ),
        (
            lambda p: p["attestation_notes"].update(
                {
                    "externally_confirmed": [
                        "exact_permission_map",
                        "exact_repository_selection",
                        "no_write_capability",
                    ]
                }
            ),
            "attestation_externally_confirmed_invalid",
        ),
        (
            lambda p: p["attestation_notes"].update({"machine_verified": ["authentication"]}),
            "attestation_machine_verified_incomplete",
        ),
        (
            lambda p: p["attestation_notes"].update(
                {"permissions_source": "operator_declared_ui_confirmed"}
            ),
            "attestation_permissions_source_invalid",
        ),
        # ---- attestation: probes -----------------------------------------
        (
            lambda p: _drop(p, "attestation_notes", "probes"),
            "attestation_probes_missing",
        ),
        (
            lambda p: p["attestation_notes"]["probes"].update({"auth_probe_status": 403}),
            "attestation_auth_probe_invalid",
        ),
        (
            lambda p: p["attestation_notes"]["probes"].update(
                {"oauth_scopes_header_present": True}
            ),
            "attestation_oauth_scopes_header_present",
        ),
        (
            lambda p: p["attestation_notes"]["probes"].update({"repository_probe_count": 2}),
            "attestation_repository_probe_count_invalid",
        ),
        (
            lambda p: p["attestation_notes"]["probes"].update({"repository_read_probes": {}}),
            "attestation_repository_read_probes_incomplete",
        ),
        (
            lambda p: p["attestation_notes"]["probes"]["repository_read_probes"][REPOSITORY].update(
                {"check_runs_status": 404}
            ),
            "attestation_read_probe_status_invalid:check_runs_status",
        ),
        (
            lambda p: p["attestation_notes"]["probes"]["repository_read_probes"][REPOSITORY].update(
                {"issues_sample_count": -1}
            ),
            "attestation_read_probe_count_invalid:issues_sample_count",
        ),
        (
            lambda p: p["attestation_notes"]["probes"].update({"installation_repository_count": 4}),
            "attestation_installation_repository_count_invalid",
        ),
        # ---- canary --------------------------------------------------------
        (
            lambda p: _drop(p, "canary"),
            "canary_missing",
        ),
        (
            lambda p: p["canary"].update({"direct_feature_enabled": False}),
            "canary_direct_feature_not_enabled",
        ),
        (
            lambda p: p["canary"].update({"canary_tool_ids": ["github.get_repo"]}),
            "canary_tool_ids_invalid",
        ),
        (
            lambda p: p["canary"].update({"canary_repositories": ["pestoura/other-repo"]}),
            "canary_repositories_not_provider_scopes",
        ),
        (
            lambda p: p["canary"].update({"wildcard_scopes": True}),
            "canary_wildcard_scopes",
        ),
        # ---- window / mutation provenance ----------------------------------
        (
            lambda p: _drop(p, "window_integrity_basis"),
            "window_integrity_basis_missing",
        ),
        (
            lambda p: p["window_integrity_basis"].update(
                {"direct_mutation_basis": "operator_asserted"}
            ),
            "direct_mutation_basis_invalid",
        ),
        (
            lambda p: p["window_integrity_basis"].update({"shadow_mutation_basis": "none"}),
            "shadow_mutation_basis_unproven",
        ),
        (
            lambda p: p["window_integrity_basis"].update({"shadow_mutation_basis": "unknown"}),
            "shadow_mutation_basis_unproven",
        ),
        (
            lambda p: _drop(p["samples"][0], "window_integrity"),
            "sample[0]:window_integrity_missing",
        ),
        (
            lambda p: p["samples"][0]["window_integrity"].update(
                {"direct_call_delta_exact": False}
            ),
            "sample[0]:window_integrity_invalid:direct_call_delta_exact",
        ),
        (
            lambda p: p["samples"][0]["window_integrity"].update({"attribution_ambiguity": True}),
            "sample[0]:window_attribution_ambiguous",
        ),
        (
            lambda p: p["samples"][0]["direct"].update({"mutation_basis": "operator_asserted"}),
            "sample[0]:direct_mutation_basis_invalid",
        ),
        (
            lambda p: p["samples"][0]["v1_shadow"].update(
                {"mutation_basis": "github_audit_log_reviewed"}
            ),
            "sample[0]:shadow_mutation_basis_invalid",
        ),
        # ---- token accounting source ----------------------------------------
        (
            lambda p: p["samples"][0]["v1_shadow"].update({"token_usage_source": "hermes_result"}),
            "sample[0]:shadow_token_source_not_canonical",
        ),
    ],
)
def test_validator_requires_provenance_and_fails_closed(
    mutator: Any,
    failure: str,
) -> None:
    payload = copy.deepcopy(valid_evidence())
    mutator(payload)
    assert failure in _validate(payload)


def test_forged_least_privilege_without_provenance_is_blocked() -> None:
    """A hand-written document with the happy booleans must not be accepted."""
    payload = copy.deepcopy(valid_evidence())
    for key in ("attestation_notes", "canary", "window_integrity_basis"):
        payload.pop(key, None)
    for sample in payload["samples"]:
        sample.pop("window_integrity", None)
    assert payload["github_provider"]["least_privilege"] is True
    assert payload["samples"][0]["contaminated_window"] is False
    failures = _validate(payload)
    assert "attestation_notes_missing" in failures
    assert "canary_missing" in failures
    assert "window_integrity_basis_missing" in failures


def test_exact_three_repetitions_per_tool_are_required() -> None:
    payload = valid_evidence()
    payload["samples"] = payload["samples"][:-1]
    failures = _validate(payload)
    assert "sample_count_invalid" in failures
    assert "tool_sample_count_invalid:github.search" in failures
    assert "tool_repetitions_incomplete:github.search" in failures


def test_direct_result_must_not_expand_provider_payload() -> None:
    payload = valid_evidence()
    payload["samples"][0]["direct"]["raw_bytes"] = 100
    payload["samples"][0]["direct"]["returned_bytes"] = 101
    assert "sample[0]:direct_shaping_expands_payload" in _validate(payload)


def test_shadow_token_accounting_must_be_real_and_consistent() -> None:
    payload = valid_evidence()
    payload["samples"][0]["v1_shadow"]["hermes_llm_tokens"] = {
        "input": 100,
        "output": 50,
        "total": 120,
    }
    assert "sample[0]:shadow_token_accounting_inconsistent" in _validate(payload)


def test_template_data_is_not_retained_as_repository_evidence() -> None:
    evidence_dir = ROOT / "docs" / "v2" / "evidence"
    forbidden_names = {
        "phase2-connected-direct-read-acceptance.json",
        "phase2-direct-read-gate.json",
    }
    assert not forbidden_names & {path.name for path in evidence_dir.glob("*.json")}
