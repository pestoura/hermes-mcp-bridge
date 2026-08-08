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
        "direct": {
            "success": True,
            "latency_ms": 12.5,
            "provider_api_calls": 1,
            "hermes_upstream_calls": 0,
            "hermes_llm_tokens": {"input": 0, "output": 0, "total": 0},
            "raw_bytes": 1200,
            "returned_bytes": 420,
            "mutation_observed": False,
            "redirect_followed": False,
        },
        "v1_shadow": {
            "success": True,
            "latency_ms": 8100.0,
            "hermes_llm_tokens": {"input": 1000, "output": 75, "total": 1075},
            "token_usage_estimated": False,
            "token_usage_source": "hermes_result",
            "mutation_observed": False,
        },
        "comparison": {
            "semantic_match": True,
            "direct_normalized_sha256": DIGEST,
            "v1_normalized_sha256": DIGEST,
        },
    }


def valid_evidence() -> dict[str, Any]:
    samples = [
        _sample(tool_id, repetition)
        for tool_id in EXPECTED_TOOLS
        for repetition in range(1, 4)
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
            lambda p: p["samples"][0]["direct"].update(
                {"hermes_upstream_calls": 1}
            ),
            "sample[0]:direct_hermes_upstream_not_zero",
        ),
        (
            lambda p: p["samples"][0]["direct"].update(
                {"hermes_llm_tokens": {"input": 1, "output": 0, "total": 1}}
            ),
            "sample[0]:direct_llm_tokens_not_zero",
        ),
        (
            lambda p: p["samples"][0]["direct"].update(
                {"provider_api_calls": 2}
            ),
            "sample[0]:direct_api_calls_not_one",
        ),
        (
            lambda p: p["samples"][0]["v1_shadow"].update(
                {"token_usage_estimated": True}
            ),
            "sample[0]:shadow_token_usage_estimated",
        ),
        (
            lambda p: p["samples"][0]["comparison"].update(
                {"semantic_match": False}
            ),
            "sample[0]:semantic_mismatch",
        ),
        (
            lambda p: p["samples"][0]["comparison"].update(
                {"v1_normalized_sha256": "b" * 64}
            ),
            "sample[0]:normalized_digest_mismatch",
        ),
        (
            lambda p: p["samples"][0].update(
                {"repository": "pestoura/not-authorized"}
            ),
            "sample[0]:repository_out_of_scope",
        ),
        (
            lambda p: p["samples"][0]["direct"].update(
                {"mutation_observed": True}
            ),
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
