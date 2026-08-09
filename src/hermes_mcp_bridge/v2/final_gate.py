"""OUTER final gate for the V2 Phase 2 connected acceptance.

This module is additive. It does **not** replace or relax the existing inner
connected launcher/gate, which remains the semantic/economics gate and stays
independently debuggable. The outer gate simply refuses to call the campaign
formally ``ACCEPTED`` unless, on top of an inner ``DIRECT_READ_ACCEPTED``:

* every accepted sample carries an internal-tool provenance PASS;
* a real out-of-band state-integrity measurement proves absolute zero delta on
  the **real** Hermes state database around the inner run;
* the shadow (disposable) state database is proven to have been active;
* the measurement window encloses the inner samples;
* no path, row content, salt or session id was ever persisted.

Nothing here can manufacture evidence: every field must be present, of the
right type and consistent across documents, otherwise the gate emits stable
sanitized reasons and blocks.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final

FINAL_EVIDENCE_SCHEMA: Final[str] = "hermes-v2-phase2-final-acceptance/1"
FINAL_MANIFEST_SCHEMA: Final[str] = "hermes-v2-phase2-final-manifest/1"
STATE_INTEGRITY_DOC_SCHEMA: Final[str] = (
    "hermes-v2-phase2-final-state-integrity/1"
)

STATUS_ACCEPTED: Final[str] = "ACCEPTED"
STATUS_BLOCKED: Final[str] = "BLOCKED"

INNER_ACCEPTED_STATUS: Final[str] = "DIRECT_READ_ACCEPTED"

EXPECTED_SAMPLE_COUNT: Final[int] = 15
MIN_TOKEN_REDUCTION_PERCENT: Final[float] = 80.0
TOKEN_MEASUREMENT_MODE: Final[str] = "empirical"

#: Real Hermes tables whose row deltas must all be exactly zero.
REQUIRED_ZERO_DELTA_TABLES: Final[tuple[str, ...]] = (
    "sessions",
    "messages",
    "session_model_usage",
)

_SHA40_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

#: Keys that must never appear anywhere in the final evidence tree.
FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "credential_value",
        "db_path",
        "env_var",
        "message_rows",
        "output_text",
        "password",
        "path",
        "private_key",
        "prompt_text",
        "raw_arguments",
        "raw_result",
        "raw_token",
        "row_contents",
        "salt",
        "secret",
        "secret_path",
        "session_id",
        "state_db_path",
        "token",
        "tool_call_id",
    }
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _sha256_ok(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


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


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _validate_inner(payload: dict[str, Any], failures: list[str]) -> str | None:
    inner = payload.get("inner_gate")
    if not isinstance(inner, dict):
        failures.append("inner_gate_missing")
        return None
    if inner.get("direct_read_status") != INNER_ACCEPTED_STATUS:
        failures.append("inner_gate_not_accepted")
    if inner.get("failures") not in ([], None):
        failures.append("inner_gate_reported_failures")
    commit = inner.get("source_commit")
    if not isinstance(commit, str) or _SHA40_RE.fullmatch(commit) is None:
        failures.append("inner_source_commit_invalid")
        return None
    return commit


def _validate_aggregate(payload: dict[str, Any], failures: list[str]) -> None:
    aggregate = payload.get("aggregate")
    if not isinstance(aggregate, dict):
        failures.append("aggregate_missing")
        return

    if aggregate.get("sample_count") != EXPECTED_SAMPLE_COUNT:
        failures.append("sample_count_invalid")
    if aggregate.get("successful_samples") != EXPECTED_SAMPLE_COUNT:
        failures.append("successful_samples_invalid")
    if aggregate.get("semantic_matches") != EXPECTED_SAMPLE_COUNT:
        failures.append("semantic_matches_invalid")
    if aggregate.get("provenance_pass") != EXPECTED_SAMPLE_COUNT:
        failures.append("provenance_pass_invalid")
    if aggregate.get("provenance_fail") != 0:
        failures.append("provenance_fail_present")
    if aggregate.get("token_measurement_mode") != TOKEN_MEASUREMENT_MODE:
        failures.append("token_measurement_mode_invalid")
    if aggregate.get("direct_total_tokens") != 0:
        failures.append("direct_total_tokens_not_zero")
    agentic: Any = aggregate.get("agentic_total_tokens")
    if not isinstance(agentic, int) or isinstance(agentic, bool) or agentic <= 0:
        failures.append("agentic_total_tokens_not_positive")
    reduction: Any = aggregate.get("token_reduction_percent")
    if (
        not isinstance(reduction, int | float)
        or isinstance(reduction, bool)
        or reduction < MIN_TOKEN_REDUCTION_PERCENT
    ):
        failures.append("token_reduction_below_threshold")
    if aggregate.get("direct_provider_api_calls") != EXPECTED_SAMPLE_COUNT:
        failures.append("direct_provider_api_calls_invalid")
    if aggregate.get("mutations_observed") != 0:
        failures.append("mutations_observed")


def _validate_provenance(payload: dict[str, Any], failures: list[str]) -> None:
    records = payload.get("provenance")
    if not isinstance(records, list):
        failures.append("provenance_records_missing")
        return
    if len(records) != EXPECTED_SAMPLE_COUNT:
        failures.append("provenance_record_count_invalid")
    for index, record in enumerate(records):
        prefix = f"provenance[{index}]"
        if not isinstance(record, dict):
            failures.append(f"{prefix}:invalid")
            continue
        if record.get("provenance_pass") is not True:
            failures.append(f"{prefix}:not_pass")
        if record.get("authorized_tool_call_count") != 1:
            failures.append(f"{prefix}:tool_call_count_invalid")
        if record.get("unauthorized_tool_calls_observed") is not False:
            failures.append(f"{prefix}:unauthorized_tool_call")
        if record.get("internal_matches_direct") is not True:
            failures.append(f"{prefix}:internal_digest_mismatch")
        if not _sha256_ok(record.get("internal_normalized_sha256")):
            failures.append(f"{prefix}:internal_digest_invalid")
        if not _sha256_ok(record.get("direct_normalized_sha256")):
            failures.append(f"{prefix}:direct_digest_invalid")
        if not isinstance(record.get("normalization_profile_id"), str):
            failures.append(f"{prefix}:normalization_profile_missing")
        if record.get("blockers") not in ([], None):
            failures.append(f"{prefix}:blockers_present")
        for key in (
            "tool_call_id_stored",
            "raw_arguments_stored",
            "raw_result_stored",
            "session_id_stored",
            "message_rows_stored",
        ):
            if record.get(key) is not False:
                failures.append(f"{prefix}:privacy_invalid:{key}")


def _validate_state_integrity(payload: dict[str, Any], failures: list[str]) -> str | None:
    state = payload.get("state_integrity")
    if not isinstance(state, dict):
        # A missing or unmeasurable state-integrity document is a hard block.
        failures.append("state_integrity_document_missing")
        return None
    if state.get("schema") != STATE_INTEGRITY_DOC_SCHEMA:
        failures.append("state_integrity_schema_invalid")
    if state.get("measured_out_of_band") is not True:
        failures.append("state_integrity_not_out_of_band")
    if state.get("control_activity_detected") is not False:
        failures.append("state_integrity_control_activity_detected")
    if state.get("exclusions_applied") is not False:
        failures.append("state_integrity_exclusions_applied")
    if state.get("read_only") is not True:
        failures.append("state_integrity_not_read_only")
    if state.get("measurement_self_write_observed") is not False:
        failures.append("state_integrity_measurement_wrote")

    before = state.get("fingerprint_before")
    after = state.get("fingerprint_after")
    if not _sha256_ok(before) or not _sha256_ok(after):
        failures.append("state_fingerprint_invalid")
    elif before != after:
        failures.append("state_fingerprint_mismatch")

    if state.get("user_version_changed") is not False:
        failures.append("state_user_version_changed")
    if state.get("sqlite_schema_version_changed") is not False:
        failures.append("state_schema_version_changed")
    if state.get("size_changed") is not False:
        failures.append("state_size_changed")
    if state.get("mtime_changed") is not False:
        failures.append("state_mtime_changed")

    deltas = state.get("row_deltas")
    if not isinstance(deltas, dict):
        failures.append("state_row_deltas_missing")
    else:
        if set(deltas) != set(REQUIRED_ZERO_DELTA_TABLES):
            failures.append("state_row_deltas_incomplete")
        for table in REQUIRED_ZERO_DELTA_TABLES:
            value = deltas.get(table)
            if value != 0:
                failures.append(f"state_row_delta_nonzero:{table}")

    if state.get("shadow_state_activity_observed") is not True:
        failures.append("shadow_state_activity_not_observed")
    shadow_delta: Any = state.get("shadow_row_count_delta")
    if (
        not isinstance(shadow_delta, int)
        or isinstance(shadow_delta, bool)
        or shadow_delta <= 0
    ):
        failures.append("shadow_row_count_delta_not_positive")

    if state.get("paths_stored") is not False:
        failures.append("state_paths_stored")
    if state.get("row_contents_stored") is not False:
        failures.append("state_row_contents_stored")

    commit = state.get("source_commit")
    if not isinstance(commit, str) or _SHA40_RE.fullmatch(commit) is None:
        failures.append("state_source_commit_invalid")
        return None
    return commit


def _validate_window(payload: dict[str, Any], failures: list[str]) -> None:
    state = payload.get("state_integrity")
    if not isinstance(state, dict):
        return
    pre = _parse_instant(state.get("measured_before_at"))
    post = _parse_instant(state.get("measured_after_at"))
    inner = payload.get("inner_gate")
    started = _parse_instant(inner.get("started_at")) if isinstance(inner, dict) else None
    finished = (
        _parse_instant(inner.get("finished_at")) if isinstance(inner, dict) else None
    )
    if pre is None or post is None:
        failures.append("state_measurement_window_missing")
        return
    if started is None or finished is None:
        failures.append("inner_window_missing")
        return
    if pre > started or finished > post:
        failures.append("state_window_does_not_enclose_samples")


def _validate_commits(
    payload: dict[str, Any],
    inner_commit: str | None,
    state_commit: str | None,
    failures: list[str],
) -> None:
    commit = payload.get("source_commit")
    if not isinstance(commit, str) or _SHA40_RE.fullmatch(commit) is None:
        failures.append("source_commit_invalid")
        return
    for other in (inner_commit, state_commit):
        if other is not None and other != commit:
            failures.append("source_commit_inconsistent")
            return


def _validate_privacy(payload: dict[str, Any], failures: list[str]) -> None:
    privacy = payload.get("privacy")
    expected = {
        "paths_stored": False,
        "row_contents_stored": False,
        "raw_results_stored": False,
        "session_ids_stored": False,
        "salt_stored": False,
    }
    if privacy != expected:
        failures.append("privacy_contract_not_met")
    forbidden = sorted(FORBIDDEN_KEYS & _walk_keys(payload))
    if forbidden:
        failures.append("forbidden_evidence_keys:" + ",".join(forbidden))


def validate_final_evidence(payload: Any) -> list[str]:
    """Return sorted stable failure reasons; empty means ``ACCEPTED``."""
    failures: list[str] = []
    if not isinstance(payload, dict):
        return ["final_evidence_not_object"]
    if payload.get("schema") != FINAL_EVIDENCE_SCHEMA:
        failures.append("invalid_schema")

    inner_commit = _validate_inner(payload, failures)
    _validate_aggregate(payload, failures)
    _validate_provenance(payload, failures)
    state_commit = _validate_state_integrity(payload, failures)
    _validate_window(payload, failures)
    _validate_commits(payload, inner_commit, state_commit, failures)
    _validate_privacy(payload, failures)
    return sorted(set(failures))


def final_manifest(payload: Any, failures: list[str]) -> dict[str, Any]:
    """Build the sanitized final manifest for a validated evidence document."""
    source_commit = payload.get("source_commit") if isinstance(payload, dict) else None
    return {
        "schema": FINAL_MANIFEST_SCHEMA,
        "overall_status": STATUS_ACCEPTED if not failures else STATUS_BLOCKED,
        "reasons": list(failures),
        "source_commit": source_commit if isinstance(source_commit, str) else None,
        "inner_gate_required": True,
        "outer_gate_required": True,
    }


__all__ = [
    "EXPECTED_SAMPLE_COUNT",
    "FINAL_EVIDENCE_SCHEMA",
    "FINAL_MANIFEST_SCHEMA",
    "FORBIDDEN_KEYS",
    "INNER_ACCEPTED_STATUS",
    "MIN_TOKEN_REDUCTION_PERCENT",
    "REQUIRED_ZERO_DELTA_TABLES",
    "STATE_INTEGRITY_DOC_SCHEMA",
    "STATUS_ACCEPTED",
    "STATUS_BLOCKED",
    "TOKEN_MEASUREMENT_MODE",
    "final_manifest",
    "validate_final_evidence",
]
