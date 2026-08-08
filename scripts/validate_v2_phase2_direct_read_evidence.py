#!/usr/bin/env python3
"""Fail-closed validator for V2 Phase 2 connected GitHub DIRECT evidence.

This validator deliberately cannot manufacture evidence. It accepts only a
connected Jarvas evidence document proving the five DIRECT read operations,
least-privilege GitHub provider readiness, V1 shadow equivalence and zero Hermes
LLM use on the DIRECT path.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA = "hermes-v2-phase2-direct-read-acceptance/1"
EXPECTED_COLLECTION_GATE = "DIRECT_READ_EVIDENCE_COLLECTED"
ACCEPTED_GATE = "DIRECT_READ_ACCEPTED"
BLOCKED_GATE = "DIRECT_READ_BLOCKED"

EXPECTED_TOOLS = (
    "github.get_checks",
    "github.get_issue",
    "github.get_pr",
    "github.get_repo",
    "github.search",
)
REPETITIONS_PER_TOOL = 3
EXPECTED_SAMPLE_COUNT = len(EXPECTED_TOOLS) * REPETITIONS_PER_TOOL

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
)

EXPECTED_PERMISSIONS = {
    "checks": "read",
    "issues": "read",
    "metadata": "read",
    "pull_requests": "read",
}
ALLOWED_PROVIDER_TYPES = {"github_app", "fine_grained_token"}

FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "credential_value",
        "env_var",
        "output_text",
        "password",
        "private_key",
        "prompt_text",
        "raw_token",
        "secret",
        "secret_path",
        "token",
    }
)


def _is_positive_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and value > 0
    )


def _is_non_negative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _walk_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).lower())
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys


def _repository_ok(value: Any) -> bool:
    if not isinstance(value, str) or not _REPOSITORY_RE.fullmatch(value):
        return False
    return not any(token in value for token in ("*", "?", "[", "]", "\\"))


def _sha256_ok(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _validate_runtime(payload: dict[str, Any], failures: list[str]) -> None:
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        failures.append("runtime_missing")
        return

    expected = {
        "bridge_version": "1.0.0",
        "schema_version": "0.6.1",
        "v1_tool_count": 27,
        "jarvas_connected": True,
        "v1_path_healthy": True,
        "direct_feature_enabled": True,
        "v1_semantics_unchanged": True,
    }
    for key, expected_value in expected.items():
        if runtime.get(key) != expected_value:
            failures.append(f"runtime_invalid:{key}")

    direct_core_commit = runtime.get("direct_core_commit")
    if (
        not isinstance(direct_core_commit, str)
        or _SHA40_RE.fullmatch(direct_core_commit) is None
    ):
        failures.append("runtime_invalid:direct_core_commit")


def _validate_provider(
    payload: dict[str, Any],
    failures: list[str],
) -> set[str]:
    provider = payload.get("github_provider")
    if not isinstance(provider, dict):
        failures.append("github_provider_missing")
        return set()

    if provider.get("credential_capability") != "github.read":
        failures.append("provider_invalid:credential_capability")
    if provider.get("provider_type") not in ALLOWED_PROVIDER_TYPES:
        failures.append("provider_invalid:provider_type")
    if provider.get("authenticated") is not True:
        failures.append("provider_not_authenticated")
    if provider.get("least_privilege") is not True:
        failures.append("provider_not_least_privilege")
    if provider.get("broad_pat") is not False:
        failures.append("provider_broad_pat_not_rejected")
    if provider.get("github_api_version") != "2026-03-10":
        failures.append("provider_invalid:github_api_version")
    if provider.get("base_url") != "https://api.github.com":
        failures.append("provider_invalid:base_url")

    permissions = provider.get("permissions")
    if permissions != EXPECTED_PERMISSIONS:
        failures.append("provider_permissions_not_exact")
    if provider.get("unexpected_permissions") != []:
        failures.append("provider_has_unexpected_permissions")

    scopes = provider.get("repository_scopes")
    if not isinstance(scopes, list) or not scopes:
        failures.append("provider_repository_scopes_missing")
        return set()
    if len(scopes) != len(set(scopes)):
        failures.append("provider_repository_scopes_duplicate")
    invalid = sorted(
        str(scope) for scope in scopes if not _repository_ok(scope)
    )
    if invalid:
        failures.append("provider_repository_scope_invalid")
    return {str(scope).lower() for scope in scopes if _repository_ok(scope)}


def _validate_discovery(payload: dict[str, Any], failures: list[str]) -> None:
    discovery = payload.get("discovery")
    if not isinstance(discovery, dict):
        failures.append("discovery_missing")
        return

    expected_true = (
        "actual_jarvas_host",
        "credential_source_discovered",
        "repository_access_verified",
    )
    for key in expected_true:
        if discovery.get(key) is not True:
            failures.append(f"discovery_invalid:{key}")

    expected_false = (
        "credential_value_recorded",
        "secret_path_recorded",
        "environment_dump_recorded",
    )
    for key in expected_false:
        if discovery.get(key) is not False:
            failures.append(f"discovery_privacy_invalid:{key}")


def _validate_direct_sample(
    direct: Any,
    *,
    prefix: str,
    failures: list[str],
) -> None:
    if not isinstance(direct, dict):
        failures.append(f"{prefix}:direct_missing")
        return
    if direct.get("success") is not True:
        failures.append(f"{prefix}:direct_not_success")
    if not _is_positive_number(direct.get("latency_ms")):
        failures.append(f"{prefix}:direct_latency_invalid")
    if direct.get("provider_api_calls") != 1:
        failures.append(f"{prefix}:direct_api_calls_not_one")
    if direct.get("hermes_upstream_calls") != 0:
        failures.append(f"{prefix}:direct_hermes_upstream_not_zero")

    usage = direct.get("hermes_llm_tokens")
    if usage != {"input": 0, "output": 0, "total": 0}:
        failures.append(f"{prefix}:direct_llm_tokens_not_zero")

    raw_bytes = direct.get("raw_bytes")
    returned_bytes = direct.get("returned_bytes")
    if not _is_non_negative_int(raw_bytes) or raw_bytes <= 0:
        failures.append(f"{prefix}:direct_raw_bytes_invalid")
    if not _is_non_negative_int(returned_bytes) or returned_bytes <= 0:
        failures.append(f"{prefix}:direct_returned_bytes_invalid")
    if (
        _is_non_negative_int(raw_bytes)
        and _is_non_negative_int(returned_bytes)
        and returned_bytes > raw_bytes
    ):
        failures.append(f"{prefix}:direct_shaping_expands_payload")

    if direct.get("mutation_observed") is not False:
        failures.append(f"{prefix}:direct_mutation_observed")
    if direct.get("redirect_followed") is not False:
        failures.append(f"{prefix}:direct_redirect_followed")


def _validate_shadow_sample(
    shadow: Any,
    *,
    prefix: str,
    failures: list[str],
) -> None:
    if not isinstance(shadow, dict):
        failures.append(f"{prefix}:shadow_missing")
        return
    if shadow.get("success") is not True:
        failures.append(f"{prefix}:shadow_not_success")
    if not _is_positive_number(shadow.get("latency_ms")):
        failures.append(f"{prefix}:shadow_latency_invalid")

    usage = shadow.get("hermes_llm_tokens")
    if not isinstance(usage, dict):
        failures.append(f"{prefix}:shadow_tokens_missing")
    else:
        for name in ("input", "output", "total"):
            if not _is_non_negative_int(usage.get(name)):
                failures.append(f"{prefix}:shadow_tokens_invalid:{name}")
        if _is_non_negative_int(usage.get("total")) and usage["total"] <= 0:
            failures.append(f"{prefix}:shadow_total_tokens_not_positive")
        if (
            _is_non_negative_int(usage.get("input"))
            and _is_non_negative_int(usage.get("output"))
            and _is_non_negative_int(usage.get("total"))
            and usage["total"] < usage["input"] + usage["output"]
        ):
            failures.append(f"{prefix}:shadow_token_accounting_inconsistent")

    if shadow.get("token_usage_estimated") is not False:
        failures.append(f"{prefix}:shadow_token_usage_estimated")
    source = shadow.get("token_usage_source")
    if not isinstance(source, str) or not source.strip():
        failures.append(f"{prefix}:shadow_token_source_missing")
    if shadow.get("mutation_observed") is not False:
        failures.append(f"{prefix}:shadow_mutation_observed")


def _validate_comparison(
    comparison: Any,
    *,
    prefix: str,
    failures: list[str],
) -> None:
    if not isinstance(comparison, dict):
        failures.append(f"{prefix}:comparison_missing")
        return
    if comparison.get("semantic_match") is not True:
        failures.append(f"{prefix}:semantic_mismatch")
    direct_digest = comparison.get("direct_normalized_sha256")
    shadow_digest = comparison.get("v1_normalized_sha256")
    if not _sha256_ok(direct_digest):
        failures.append(f"{prefix}:direct_digest_invalid")
    if not _sha256_ok(shadow_digest):
        failures.append(f"{prefix}:shadow_digest_invalid")
    if (
        _sha256_ok(direct_digest)
        and _sha256_ok(shadow_digest)
        and direct_digest != shadow_digest
    ):
        failures.append(f"{prefix}:normalized_digest_mismatch")


def _validate_samples(
    payload: dict[str, Any],
    repository_scopes: set[str],
    failures: list[str],
) -> Counter[str]:
    samples = payload.get("samples")
    if not isinstance(samples, list):
        failures.append("samples_missing")
        return Counter()
    if len(samples) != EXPECTED_SAMPLE_COUNT:
        failures.append("sample_count_invalid")

    tool_counts: Counter[str] = Counter()
    repetition_pairs: set[tuple[str, int]] = set()

    for index, sample in enumerate(samples):
        prefix = f"sample[{index}]"
        if not isinstance(sample, dict):
            failures.append(f"{prefix}:invalid")
            continue

        tool_id = sample.get("tool_id")
        if tool_id not in EXPECTED_TOOLS:
            failures.append(f"{prefix}:tool_invalid")
            continue
        tool_counts[tool_id] += 1

        repetition = sample.get("repetition")
        if (
            isinstance(repetition, bool)
            or not isinstance(repetition, int)
            or repetition not in range(1, REPETITIONS_PER_TOOL + 1)
        ):
            failures.append(f"{prefix}:repetition_invalid")
        else:
            pair = (tool_id, repetition)
            if pair in repetition_pairs:
                failures.append(f"{prefix}:repetition_duplicate")
            repetition_pairs.add(pair)

        repository = sample.get("repository")
        if not _repository_ok(repository):
            failures.append(f"{prefix}:repository_invalid")
        elif str(repository).lower() not in repository_scopes:
            failures.append(f"{prefix}:repository_out_of_scope")

        if sample.get("connected_jarvas") is not True:
            failures.append(f"{prefix}:not_connected_jarvas")
        if sample.get("contaminated_window") is not False:
            failures.append(f"{prefix}:contaminated_window")

        _validate_direct_sample(
            sample.get("direct"),
            prefix=prefix,
            failures=failures,
        )
        _validate_shadow_sample(
            sample.get("v1_shadow"),
            prefix=prefix,
            failures=failures,
        )
        _validate_comparison(
            sample.get("comparison"),
            prefix=prefix,
            failures=failures,
        )

    for tool_id in EXPECTED_TOOLS:
        if tool_counts[tool_id] != REPETITIONS_PER_TOOL:
            failures.append(f"tool_sample_count_invalid:{tool_id}")
        expected_pairs = {
            (tool_id, repetition)
            for repetition in range(1, REPETITIONS_PER_TOOL + 1)
        }
        if not expected_pairs.issubset(repetition_pairs):
            failures.append(f"tool_repetitions_incomplete:{tool_id}")

    return tool_counts


def _validate_aggregate(
    payload: dict[str, Any],
    tool_counts: Counter[str],
    failures: list[str],
) -> None:
    aggregate = payload.get("aggregate")
    if not isinstance(aggregate, dict):
        failures.append("aggregate_missing")
        return

    expected_counts = {tool: REPETITIONS_PER_TOOL for tool in EXPECTED_TOOLS}
    if aggregate.get("sample_count") != EXPECTED_SAMPLE_COUNT:
        failures.append("aggregate_sample_count_invalid")
    if aggregate.get("successful_samples") != EXPECTED_SAMPLE_COUNT:
        failures.append("aggregate_success_count_invalid")
    if aggregate.get("semantic_matches") != EXPECTED_SAMPLE_COUNT:
        failures.append("aggregate_semantic_matches_invalid")
    if aggregate.get("direct_provider_api_calls") != EXPECTED_SAMPLE_COUNT:
        failures.append("aggregate_direct_api_calls_invalid")
    if aggregate.get("direct_hermes_upstream_calls") != 0:
        failures.append("aggregate_direct_hermes_upstream_not_zero")
    if aggregate.get("direct_hermes_llm_tokens") != 0:
        failures.append("aggregate_direct_llm_tokens_not_zero")
    shadow_tokens = aggregate.get("v1_shadow_hermes_llm_tokens")
    if not _is_non_negative_int(shadow_tokens) or shadow_tokens <= 0:
        failures.append("aggregate_shadow_tokens_not_positive")
    if aggregate.get("mutations_observed") != 0:
        failures.append("aggregate_mutations_observed")
    if aggregate.get("contaminated_windows") != 0:
        failures.append("aggregate_contaminated_windows")
    if aggregate.get("tool_sample_counts") != expected_counts:
        failures.append("aggregate_tool_counts_invalid")
    if dict(tool_counts) != expected_counts:
        failures.append("observed_tool_counts_invalid")


def _validate_privacy(payload: dict[str, Any], failures: list[str]) -> None:
    privacy = payload.get("privacy")
    expected = {
        "credential_values_stored": False,
        "environment_dump_stored": False,
        "outputs_stored": False,
        "prompts_stored": False,
        "secret_paths_stored": False,
    }
    if privacy != expected:
        failures.append("privacy_contract_not_met")

    forbidden = sorted(FORBIDDEN_KEYS & _walk_keys(payload))
    if forbidden:
        failures.append("forbidden_evidence_keys:" + ",".join(forbidden))


def validate_evidence(payload: dict[str, Any]) -> list[str]:
    """Return stable failure codes; an empty list is DIRECT_READ_ACCEPTED."""
    failures: list[str] = []

    if payload.get("schema") != EVIDENCE_SCHEMA:
        failures.append("invalid_schema")
    if payload.get("gate") != EXPECTED_COLLECTION_GATE:
        failures.append("invalid_collection_gate")

    source_commit = payload.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or _SHA40_RE.fullmatch(source_commit) is None
    ):
        failures.append("invalid_source_commit")

    _validate_runtime(payload, failures)
    repository_scopes = _validate_provider(payload, failures)
    _validate_discovery(payload, failures)
    tool_counts = _validate_samples(payload, repository_scopes, failures)
    _validate_aggregate(payload, tool_counts, failures)
    _validate_privacy(payload, failures)

    return sorted(set(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    payload = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("evidence must be a JSON object")

    failures = validate_evidence(payload)
    result = {
        "gate": ACCEPTED_GATE if not failures else BLOCKED_GATE,
        "failures": failures,
        "source_commit": payload.get("source_commit"),
    }
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
