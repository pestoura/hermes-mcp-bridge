#!/usr/bin/env python3
"""Fail-closed validator for Hermes MCP Bridge V2 Phase 1 registry evidence."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA = "hermes-v2-phase1-registry-acceptance/1"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_REQUIREMENTS = frozenset(
    {
        "V2-FR-012",
        "V2-FR-013",
        "V2-FR-014",
        "V2-FR-017",
        "V2-FR-021",
        "V2-SEC-001",
        "V2-SEC-002",
        "V2-SEC-006",
        "V2-SEC-013",
        "V2-SEC-014",
        "V2-SEC-019",
        "V2-SEC-020",
        "V2-NFR-013",
        "V2-NFR-017",
    }
)

REQUIRED_CHECKS = frozenset(
    {
        "canonical_snapshot_deterministic",
        "material_change_changes_snapshot_hash",
        "capability_health_model_exact",
        "unknown_tool_denied",
        "missing_policy_denied",
        "missing_credential_denied",
        "degraded_capability_denied",
        "destructive_t4_denied_before_allow",
        "approval_required_projected_explicitly",
        "projection_authorized_subset_only",
        "projection_field_allowlist_exact",
        "editorial_and_operator_text_not_serialized",
        "materialized_sensitive_schema_rejected",
        "sensitive_field_name_without_literal_allowed",
        "wildcard_policy_rejected",
        "credential_contract_status_only",
        "v1_contract_surface_unchanged",
        "v1_modules_do_not_import_v2",
    }
)

EXPECTED_HEALTH_MATRIX = {
    "CONFIGURED": {"configured": True, "available": False, "healthy": False, "ready": False},
    "AVAILABLE": {"configured": True, "available": True, "healthy": False, "ready": False},
    "HEALTHY": {"configured": True, "available": True, "healthy": True, "ready": False},
    "READY": {"configured": True, "available": True, "healthy": True, "ready": True},
    "DEGRADED": {"configured": True, "available": True, "healthy": False, "ready": False},
    "UNAVAILABLE": {"configured": True, "available": False, "healthy": False, "ready": False},
    "DENIED": {"configured": True, "available": False, "healthy": False, "ready": False},
}

EXPECTED_PROJECTED_FIELDS = [
    "execution_mode",
    "input_schema",
    "mutation_class",
    "operation",
    "output_schema",
    "provider",
    "read_only",
    "requires_approval",
    "result_shaping",
    "security_tier",
    "timeout_seconds",
    "tool_id",
    "version",
]

FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "authorization",
        "cookie",
        "private_key",
        "credential_value",
        "secret_path",
        "env_var",
    }
)


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


def _decision(payload: dict[str, Any], tool_id: str) -> tuple[str | None, str | None]:
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        return None, None
    decisions = policy.get("decisions")
    if not isinstance(decisions, dict):
        return None, None
    item = decisions.get(tool_id)
    if not isinstance(item, dict):
        return None, None
    return item.get("decision"), item.get("reason_code")


def validate_evidence(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    if payload.get("schema") != EVIDENCE_SCHEMA:
        failures.append("invalid_schema")
    if payload.get("gate") != "REGISTRY_EVIDENCE_COLLECTED":
        failures.append("invalid_collection_gate")

    source_commit = payload.get("source_commit")
    if not isinstance(source_commit, str) or not _SHA40_RE.fullmatch(source_commit):
        failures.append("invalid_source_commit")

    requirements = payload.get("requirements")
    if not isinstance(requirements, list):
        failures.append("requirements_missing")
    else:
        missing = REQUIRED_REQUIREMENTS - set(requirements)
        if missing:
            failures.append("requirements_missing:" + ",".join(sorted(missing)))

    checks = payload.get("checks")
    if not isinstance(checks, dict):
        failures.append("checks_missing")
    else:
        missing_checks = REQUIRED_CHECKS - set(checks)
        if missing_checks:
            failures.append("checks_missing:" + ",".join(sorted(missing_checks)))
        unexpected = set(checks) - REQUIRED_CHECKS
        if unexpected:
            failures.append("checks_unexpected:" + ",".join(sorted(unexpected)))
        for name in sorted(REQUIRED_CHECKS):
            if checks.get(name) is not True:
                failures.append(f"check_failed:{name}")

    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        failures.append("snapshot_missing")
    else:
        if snapshot.get("schema_version") != "v2.phase1.1":
            failures.append("snapshot_schema_version_invalid")
        digest = snapshot.get("capability_snapshot_hash")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            failures.append("snapshot_hash_invalid")
        if snapshot.get("tool_count") != 6:
            failures.append("snapshot_tool_count_invalid")
        if snapshot.get("capability_count") != 5:
            failures.append("snapshot_capability_count_invalid")
        if snapshot.get("deterministic_under_reordering") is not True:
            failures.append("snapshot_not_deterministic")
        if snapshot.get("material_change_detected") is not True:
            failures.append("snapshot_material_change_not_detected")

    if payload.get("capability_state_matrix") != EXPECTED_HEALTH_MATRIX:
        failures.append("capability_state_matrix_invalid")

    expected_decisions = {
        "github.get_checks": ("DENY", "CREDENTIAL_CAPABILITY_UNKNOWN"),
        "github.get_issue": ("DENY", "MISSING_POLICY_RULE"),
        "github.get_pr": ("ALLOW", "ALLOWED"),
        "github.create_pr": ("APPROVAL_REQUIRED", "APPROVAL_REQUIRED_BY_RULE"),
        "github.delete_repo": ("DENY", "DESTRUCTIVE_DENIED_BY_DEFAULT"),
        "system.status": ("DENY", "CAPABILITY_NOT_READY"),
    }
    for tool_id, expected in expected_decisions.items():
        if _decision(payload, tool_id) != expected:
            failures.append(f"policy_decision_invalid:{tool_id}")

    policy = payload.get("policy")
    unknown = policy.get("unknown_tool") if isinstance(policy, dict) else None
    if not isinstance(unknown, dict):
        failures.append("unknown_tool_evidence_missing")
    elif (
        unknown.get("decision") != "DENY"
        or unknown.get("reason_code") != "UNKNOWN_TOOL"
    ):
        failures.append("unknown_tool_not_denied")

    projection = payload.get("projection")
    if not isinstance(projection, dict):
        failures.append("projection_missing")
    else:
        if projection.get("tool_ids") != ["github.create_pr", "github.get_pr"]:
            failures.append("projection_tool_ids_invalid")
        if projection.get("projected_fields") != EXPECTED_PROJECTED_FIELDS:
            failures.append("projection_fields_invalid")
        projection_hash = projection.get("projection_hash")
        if not isinstance(projection_hash, str) or not _SHA256_RE.fullmatch(projection_hash):
            failures.append("projection_hash_invalid")
        snapshot_hash = (
            snapshot.get("capability_snapshot_hash") if isinstance(snapshot, dict) else None
        )
        if projection.get("capability_snapshot_hash") != snapshot_hash:
            failures.append("projection_snapshot_hash_mismatch")
        excluded = projection.get("excluded_reason_codes")
        expected_excluded = {
            "github.delete_repo": "DESTRUCTIVE_DENIED_BY_DEFAULT",
            "github.get_checks": "CREDENTIAL_CAPABILITY_UNKNOWN",
            "github.get_issue": "MISSING_POLICY_RULE",
            "system.status": "CAPABILITY_NOT_READY",
        }
        if excluded != expected_excluded:
            failures.append("projection_excluded_reasons_invalid")

    credentials = payload.get("credentials")
    if not isinstance(credentials, dict):
        failures.append("credentials_evidence_missing")
    else:
        if credentials.get("status_fields") != [
            "capability_id",
            "provider",
            "state",
            "version",
        ]:
            failures.append("credential_status_fields_invalid")
        if credentials.get("real_backend_present") is not False:
            failures.append("phase1_scope_creep_real_credential_backend")

    v1 = payload.get("v1")
    if not isinstance(v1, dict):
        failures.append("v1_evidence_missing")
    else:
        if v1.get("tool_contract_count") != 27:
            failures.append("v1_tool_contract_changed")
        if v1.get("v2_import_hits") != []:
            failures.append("v1_imports_v2")
        if v1.get("runtime_wiring_changed") is not False:
            failures.append("v1_runtime_wiring_changed")

    privacy = payload.get("privacy")
    if privacy != {
        "credential_values_stored": False,
        "secret_paths_stored": False,
        "editorial_text_serialized": False,
    }:
        failures.append("privacy_contract_not_met")

    forbidden_present = sorted(FORBIDDEN_KEYS & _walk_keys(payload))
    if forbidden_present:
        failures.append("forbidden_evidence_keys:" + ",".join(forbidden_present))

    return failures


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
        "gate": "REGISTRY_ACCEPTED" if not failures else "REGISTRY_BLOCKED",
        "failures": sorted(set(failures)),
        "source_commit": payload.get("source_commit"),
    }
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
