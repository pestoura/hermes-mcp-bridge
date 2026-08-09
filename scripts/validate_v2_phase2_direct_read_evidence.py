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
from datetime import datetime
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
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")

EXPECTED_PERMISSIONS = {
    "checks": "read",
    "issues": "read",
    "metadata": "read",
    "pull_requests": "read",
}
ALLOWED_PROVIDER_TYPES = {"github_app", "fine_grained_token"}

#: Sanitized external declaration schema the collector embeds under
#: ``attestation_notes.declaration``. Only this exact version is accepted.
ATTESTATION_INPUT_SCHEMA = "hermes-v2-phase2-provider-attestation/1"

#: Confirmation sources accepted per provider type. Anything else is a forged
#: or unsupported provenance claim and fails closed.
ALLOWED_CONFIRMATION_SOURCES = {
    "fine_grained_token": {"github_settings_ui"},
    "github_app": {"github_app_settings_ui", "installation_token_mint_response"},
}

#: Facts that only the operator declaration can establish. The evidence must
#: prove exactly these — no more (an extra entry would claim external
#: confirmation of something nobody confirmed) and no fewer.
REQUIRED_EXTERNALLY_CONFIRMED = {
    "exact_permission_map",
    "exact_repository_selection",
}

#: Facts the live read-only probes actually established.
REQUIRED_MACHINE_VERIFIED = {
    "authentication",
    "repository_metadata_read",
    "pull_requests_read",
    "issues_read",
    "check_runs_read",
}
#: Additional machine-verified fact required for a GitHub App installation.
APP_MACHINE_VERIFIED = "installation_repository_set"

#: ``permissions_source`` values accepted per (provider_type, confirmation
#: source). A fine-grained token can never claim a mint-response provenance.
ALLOWED_PERMISSIONS_SOURCES = {
    ("fine_grained_token", "github_settings_ui"): "operator_declared_ui_confirmed",
    ("github_app", "github_app_settings_ui"): "operator_declared_ui_confirmed",
    (
        "github_app",
        "installation_token_mint_response",
    ): "installation_token_mint_response",
}

#: Per-repository read probe fields that must each be an HTTP 200.
REQUIRED_PROBE_STATUSES = (
    "metadata_status",
    "pulls_status",
    "issues_status",
    "check_runs_status",
)
#: Per-repository read probe fields that must each be a non-negative integer.
REQUIRED_PROBE_COUNTS = (
    "pulls_sample_count",
    "issues_sample_count",
    "check_runs_total_count",
)

#: The only basis under which the DIRECT side may claim no mutation: the
#: executor is structurally restricted to GET.
DIRECT_MUTATION_BASIS = "executor_http_method_restricted_to_get"

#: Documented observational bases accepted for the V1 shadow side. ``none`` and
#: ``unknown`` are explicitly not bases and are rejected.
ALLOWED_SHADOW_MUTATION_BASES = {
    "github_audit_log_reviewed",
    "read_only_credential_enforced",
}

#: The canonical real token accounting source. Any other string — including a
#: plausible-looking one — is not the Hermes state DB and fails closed.
CANONICAL_TOKEN_USAGE_SOURCE = "hermes_state_db:session_model_usage"

#: The per-sample window integrity facts, all of which must be proven true.
REQUIRED_WINDOW_TRUE = (
    "direct_transport_dedicated",
    "direct_call_delta_exact",
    "shadow_session_scoped_accounting",
)

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
    return not isinstance(value, bool) and isinstance(value, int | float) and value > 0


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
    if not isinstance(direct_core_commit, str) or _SHA40_RE.fullmatch(direct_core_commit) is None:
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
    invalid = sorted(str(scope) for scope in scopes if not _repository_ok(scope))
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


def _validate_attestation(
    payload: dict[str, Any],
    provider_type: Any,
    repository_scopes: set[str],
    failures: list[str],
) -> None:
    """Require the provenance behind ``least_privilege`` / permission claims.

    A hand-forged document declaring ``least_privilege = true`` must not pass:
    the evidence has to carry the sanitized external declaration AND the live
    probe record that together justify it. No secret path or value is required,
    accepted or inspected here.
    """
    notes = payload.get("attestation_notes")
    if not isinstance(notes, dict):
        failures.append("attestation_notes_missing")
        return

    if notes.get("attestation_path_recorded") is not False:
        failures.append("attestation_path_recorded")

    # ---- external declaration -------------------------------------------
    declaration = notes.get("declaration")
    confirmation_source: Any = None
    if not isinstance(declaration, dict):
        failures.append("attestation_declaration_missing")
    else:
        if declaration.get("schema") != ATTESTATION_INPUT_SCHEMA:
            failures.append("attestation_declaration_schema_invalid")
        if declaration.get("confirmation") is not True:
            failures.append("attestation_declaration_not_confirmed")
        confirmation_source = declaration.get("confirmation_source")
        allowed = ALLOWED_CONFIRMATION_SOURCES.get(str(provider_type), set())
        if confirmation_source not in allowed:
            failures.append("attestation_confirmation_source_not_allowed")
        confirmed_at = declaration.get("confirmed_at")
        if not isinstance(confirmed_at, str) or not confirmed_at.strip():
            failures.append("attestation_confirmed_at_missing")
        else:
            try:
                parsed = datetime.fromisoformat(confirmed_at.strip().replace("Z", "+00:00"))
            except ValueError:
                failures.append("attestation_confirmed_at_invalid")
            else:
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    failures.append("attestation_confirmed_at_not_timezone_aware")

    # ---- what the declaration covers vs what probes proved ---------------
    externally = notes.get("externally_confirmed")
    if not isinstance(externally, list) or set(externally) != REQUIRED_EXTERNALLY_CONFIRMED:
        failures.append("attestation_externally_confirmed_invalid")

    machine = notes.get("machine_verified")
    if not isinstance(machine, list):
        failures.append("attestation_machine_verified_missing")
    else:
        observed = set(machine)
        required = set(REQUIRED_MACHINE_VERIFIED)
        if provider_type == "github_app":
            required.add(APP_MACHINE_VERIFIED)
        if not required <= observed:
            failures.append("attestation_machine_verified_incomplete")
        if observed - required - {APP_MACHINE_VERIFIED}:
            failures.append("attestation_machine_verified_unexpected")

    # ---- permissions_source coherence ------------------------------------
    expected_source = ALLOWED_PERMISSIONS_SOURCES.get(
        (str(provider_type), str(confirmation_source))
    )
    if expected_source is None:
        failures.append("attestation_permissions_source_incoherent")
    elif notes.get("permissions_source") != expected_source:
        failures.append("attestation_permissions_source_invalid")

    # ---- live probe record ------------------------------------------------
    probes = notes.get("probes")
    if not isinstance(probes, dict):
        failures.append("attestation_probes_missing")
        return

    if probes.get("auth_probe_status") != 200:
        failures.append("attestation_auth_probe_invalid")
    if probes.get("oauth_scopes_header_present") is not False:
        failures.append("attestation_oauth_scopes_header_present")
    if probes.get("repository_probe_count") != len(repository_scopes):
        failures.append("attestation_repository_probe_count_invalid")

    read_probes = probes.get("repository_read_probes")
    if not isinstance(read_probes, dict):
        failures.append("attestation_repository_read_probes_missing")
    else:
        probed = {str(key).lower() for key in read_probes}
        if probed != repository_scopes:
            failures.append("attestation_repository_read_probes_incomplete")
        for repository in sorted(read_probes):
            record = read_probes[repository]
            if not isinstance(record, dict):
                failures.append("attestation_repository_read_probe_invalid")
                continue
            for name in REQUIRED_PROBE_STATUSES:
                if record.get(name) != 200:
                    failures.append(f"attestation_read_probe_status_invalid:{name}")
            for name in REQUIRED_PROBE_COUNTS:
                if not _is_non_negative_int(record.get(name)):
                    failures.append(f"attestation_read_probe_count_invalid:{name}")

    if (
        provider_type == "fine_grained_token"
        and probes.get("fine_grained_self_enumeration_available") is not False
    ):
        failures.append("attestation_fine_grained_self_enumeration_invalid")
    if provider_type == "github_app" and probes.get("installation_repository_count") != len(
        repository_scopes
    ):
        failures.append("attestation_installation_repository_count_invalid")


def _validate_canary(
    payload: dict[str, Any],
    repository_scopes: set[str],
    failures: list[str],
) -> None:
    """Require the canary wiring that the DIRECT samples were actually taken on."""
    canary = payload.get("canary")
    if not isinstance(canary, dict):
        failures.append("canary_missing")
        return
    if canary.get("direct_feature_enabled") is not True:
        failures.append("canary_direct_feature_not_enabled")
    tool_ids = canary.get("canary_tool_ids")
    if not isinstance(tool_ids, list) or sorted(str(t) for t in tool_ids) != sorted(EXPECTED_TOOLS):
        failures.append("canary_tool_ids_invalid")
    repositories = canary.get("canary_repositories")
    if (
        not isinstance(repositories, list)
        or {str(item).lower() for item in repositories} != repository_scopes
    ):
        failures.append("canary_repositories_not_provider_scopes")
    if canary.get("wildcard_scopes") is not False:
        failures.append("canary_wildcard_scopes")


def _validate_window_basis(payload: dict[str, Any], failures: list[str]) -> str | None:
    """Require the documented top-level mutation/window provenance."""
    basis = payload.get("window_integrity_basis")
    if not isinstance(basis, dict):
        failures.append("window_integrity_basis_missing")
        return None
    if basis.get("direct_mutation_basis") != DIRECT_MUTATION_BASIS:
        failures.append("direct_mutation_basis_invalid")
    shadow_basis = basis.get("shadow_mutation_basis")
    if shadow_basis not in ALLOWED_SHADOW_MUTATION_BASES:
        failures.append("shadow_mutation_basis_unproven")
        return None
    return str(shadow_basis)


def _validate_window(sample: dict[str, Any], *, prefix: str, failures: list[str]) -> None:
    """Require the derived per-sample window facts, not a bare boolean."""
    window = sample.get("window_integrity")
    if not isinstance(window, dict):
        failures.append(f"{prefix}:window_integrity_missing")
        return
    for name in REQUIRED_WINDOW_TRUE:
        if window.get(name) is not True:
            failures.append(f"{prefix}:window_integrity_invalid:{name}")
    if window.get("attribution_ambiguity") is not False:
        failures.append(f"{prefix}:window_attribution_ambiguous")
    unexpected = set(window) - set(REQUIRED_WINDOW_TRUE) - {"attribution_ambiguity"}
    if unexpected:
        failures.append(f"{prefix}:window_integrity_unexpected_fields")


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
    if direct.get("mutation_basis") != DIRECT_MUTATION_BASIS:
        failures.append(f"{prefix}:direct_mutation_basis_invalid")
    if direct.get("redirect_followed") is not False:
        failures.append(f"{prefix}:direct_redirect_followed")


def _validate_shadow_sample(
    shadow: Any,
    *,
    prefix: str,
    shadow_mutation_basis: str | None,
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
    if shadow.get("token_usage_source") != CANONICAL_TOKEN_USAGE_SOURCE:
        failures.append(f"{prefix}:shadow_token_source_not_canonical")
    if shadow.get("mutation_observed") is not False:
        failures.append(f"{prefix}:shadow_mutation_observed")
    if shadow_mutation_basis is None or shadow.get("mutation_basis") != shadow_mutation_basis:
        failures.append(f"{prefix}:shadow_mutation_basis_invalid")


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
    if _sha256_ok(direct_digest) and _sha256_ok(shadow_digest) and direct_digest != shadow_digest:
        failures.append(f"{prefix}:normalized_digest_mismatch")


def _validate_samples(
    payload: dict[str, Any],
    repository_scopes: set[str],
    shadow_mutation_basis: str | None,
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
        _validate_window(sample, prefix=prefix, failures=failures)

        _validate_direct_sample(
            sample.get("direct"),
            prefix=prefix,
            failures=failures,
        )
        _validate_shadow_sample(
            sample.get("v1_shadow"),
            prefix=prefix,
            shadow_mutation_basis=shadow_mutation_basis,
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
            (tool_id, repetition) for repetition in range(1, REPETITIONS_PER_TOOL + 1)
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
    if not isinstance(source_commit, str) or _SHA40_RE.fullmatch(source_commit) is None:
        failures.append("invalid_source_commit")

    _validate_runtime(payload, failures)
    repository_scopes = _validate_provider(payload, failures)
    provider = payload.get("github_provider")
    provider_type = provider.get("provider_type") if isinstance(provider, dict) else None
    _validate_attestation(payload, provider_type, repository_scopes, failures)
    _validate_canary(payload, repository_scopes, failures)
    shadow_mutation_basis = _validate_window_basis(payload, failures)
    _validate_discovery(payload, failures)
    tool_counts = _validate_samples(
        payload,
        repository_scopes,
        shadow_mutation_basis,
        failures,
    )
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
