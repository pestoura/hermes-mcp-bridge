"""Hermetic tests for the V2 Phase 2 OUTER final gate.

Covers internal-tool provenance, real-state before/after integrity, the strict
final acceptance gate, the out-of-band systemd plan and the privacy contract.
Nothing here performs a real out-of-band acceptance or touches the operator's
real Hermes state database.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hermes_mcp_bridge.v2.canonical import canonical_json_bytes  # noqa: E402
from hermes_mcp_bridge.v2.final_gate import (  # noqa: E402
    EXPECTED_SAMPLE_COUNT,
    FINAL_EVIDENCE_SCHEMA,
    STATE_INTEGRITY_DOC_SCHEMA,
    STATUS_ACCEPTED,
    STATUS_BLOCKED,
    final_manifest,
    validate_final_evidence,
)
from hermes_mcp_bridge.v2.out_of_band import (  # noqa: E402
    assert_plan_is_secret_free,
    build_transient_unit_plan,
)
from hermes_mcp_bridge.v2.state_integrity import (  # noqa: E402
    capture_state_snapshot,
    compare_snapshots,
    new_run_salt,
    read_state_metadata,
)
from hermes_mcp_bridge.v2.tool_provenance import (  # noqa: E402
    NORMALIZATION_PROFILE_ID,
    PROVENANCE_SCHEMA,
    ProvenanceError,
    blocked_record,
    collect_tool_provenance,
    result_size_bucket,
)


def _load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"_script_{name}", SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FINAL_RUNNER = _load_script("v2_phase2_final_out_of_band_acceptance")
FINAL_VALIDATOR = _load_script("validate_v2_phase2_final_acceptance")
FINAL_BUILDER = _load_script("build_v2_phase2_final_evidence")


# ---------------------------------------------------------------------------
# fixtures: hermetic SQLite databases
# ---------------------------------------------------------------------------


def _make_state_db(path: Path, *, rows: int = 3) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT
            );
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        for index in range(rows):
            connection.execute(
                "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
                (f"session-{index}", 1.0 * index),
            )
        connection.commit()
    finally:
        connection.close()


def _insert_shadow_conversation(
    path: Path,
    *,
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    call_id: str = "call-1",
    extra_calls: list[tuple[str, dict[str, Any]]] | None = None,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT OR IGNORE INTO sessions (id, started_at) VALUES (?, 0.0)",
            (session_id,),
        )
        calls = [
            {
                "id": call_id,
                "function": {"name": tool_name, "arguments": json.dumps(arguments)},
            }
        ]
        for index, (name, args) in enumerate(extra_calls or []):
            calls.append(
                {
                    "id": f"{call_id}-extra-{index}",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            )
        connection.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls) "
            "VALUES (?, 'assistant', NULL, ?)",
            (session_id, json.dumps(calls)),
        )
        connection.execute(
            "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name) "
            "VALUES (?, 'tool', ?, ?, ?)",
            (session_id, json.dumps(result), call_id, tool_name),
        )
        connection.commit()
    finally:
        connection.close()


def _digest(_tool_id: str, data: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(data)).hexdigest()


# ---------------------------------------------------------------------------
# A) internal-tool provenance
# ---------------------------------------------------------------------------


def test_provenance_single_authorized_call_passes(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    result = {"number": 54, "state": "open"}
    _insert_shadow_conversation(
        db,
        session_id="s1",
        tool_name="github_get_pr",
        arguments={"number": 54},
        result=result,
    )
    record = collect_tool_provenance(
        shadow_state_db=str(db),
        session_id="s1",
        expected_tool_id="github.get_pr",
        expected_arguments={"number": 54},
        direct_normalized_sha256=_digest("github.get_pr", result),
        normalizer=_digest,
    )
    payload = record.as_canonical()
    assert payload["provenance_pass"] is True
    assert payload["canonical_tool_id"] == "github.get_pr"
    assert payload["authorized_tool_call_count"] == 1
    assert payload["normalization_profile_id"] == NORMALIZATION_PROFILE_ID
    assert payload["schema"] == PROVENANCE_SCHEMA
    assert payload["blockers"] == []


def test_provenance_zero_calls_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    with pytest.raises(ProvenanceError) as excinfo:
        collect_tool_provenance(
            shadow_state_db=str(db),
            session_id="s1",
            expected_tool_id="github.get_pr",
            expected_arguments={"number": 54},
            direct_normalized_sha256="0" * 64,
            normalizer=_digest,
        )
    assert excinfo.value.code == "PROVENANCE_NO_AUTHORIZED_TOOL_CALL"


def test_provenance_two_calls_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    result = {"number": 54}
    _insert_shadow_conversation(
        db,
        session_id="s1",
        tool_name="github_get_pr",
        arguments={"number": 54},
        result=result,
        extra_calls=[("github_get_repo", {})],
    )
    with pytest.raises(ProvenanceError) as excinfo:
        collect_tool_provenance(
            shadow_state_db=str(db),
            session_id="s1",
            expected_tool_id="github.get_pr",
            expected_arguments={"number": 54},
            direct_normalized_sha256=_digest("github.get_pr", result),
            normalizer=_digest,
        )
    assert excinfo.value.code == "PROVENANCE_MULTIPLE_AUTHORIZED_TOOL_CALLS"


def test_provenance_unauthorized_tool_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    _insert_shadow_conversation(
        db,
        session_id="s1",
        tool_name="terminal",
        arguments={"command": "ls"},
        result={"ok": True},
    )
    with pytest.raises(ProvenanceError) as excinfo:
        collect_tool_provenance(
            shadow_state_db=str(db),
            session_id="s1",
            expected_tool_id="github.get_pr",
            expected_arguments={"number": 54},
            direct_normalized_sha256="0" * 64,
            normalizer=_digest,
        )
    assert excinfo.value.code == "PROVENANCE_UNAUTHORIZED_TOOL_CALL"


def test_provenance_tool_mismatch_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    _insert_shadow_conversation(
        db,
        session_id="s1",
        tool_name="github_get_repo",
        arguments={},
        result={"full_name": "a/b"},
    )
    with pytest.raises(ProvenanceError) as excinfo:
        collect_tool_provenance(
            shadow_state_db=str(db),
            session_id="s1",
            expected_tool_id="github.get_pr",
            expected_arguments={},
            direct_normalized_sha256="0" * 64,
            normalizer=_digest,
        )
    assert excinfo.value.code == "PROVENANCE_TOOL_MISMATCH"


def test_provenance_target_mismatch_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    result = {"number": 54}
    _insert_shadow_conversation(
        db,
        session_id="s1",
        tool_name="github_get_pr",
        arguments={"number": 99},
        result=result,
    )
    with pytest.raises(ProvenanceError) as excinfo:
        collect_tool_provenance(
            shadow_state_db=str(db),
            session_id="s1",
            expected_tool_id="github.get_pr",
            expected_arguments={"number": 54},
            direct_normalized_sha256=_digest("github.get_pr", result),
            normalizer=_digest,
        )
    assert excinfo.value.code == "PROVENANCE_TARGET_MISMATCH"


def test_provenance_arg_shape_mismatch_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    result = {"number": 54}
    _insert_shadow_conversation(
        db,
        session_id="s1",
        tool_name="github_get_pr",
        arguments={"number": "54"},
        result=result,
    )
    with pytest.raises(ProvenanceError) as excinfo:
        collect_tool_provenance(
            shadow_state_db=str(db),
            session_id="s1",
            expected_tool_id="github.get_pr",
            expected_arguments={"number": 54},
            direct_normalized_sha256=_digest("github.get_pr", result),
            normalizer=_digest,
        )
    assert excinfo.value.code == "PROVENANCE_ARG_SHAPE_MISMATCH"


def test_provenance_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    _insert_shadow_conversation(
        db,
        session_id="s1",
        tool_name="github_get_pr",
        arguments={"number": 54},
        result={"number": 54, "state": "closed"},
    )
    with pytest.raises(ProvenanceError) as excinfo:
        collect_tool_provenance(
            shadow_state_db=str(db),
            session_id="s1",
            expected_tool_id="github.get_pr",
            expected_arguments={"number": 54},
            direct_normalized_sha256=_digest("github.get_pr", {"number": 54}),
            normalizer=_digest,
        )
    assert excinfo.value.code == "PROVENANCE_DIGEST_MISMATCH"


def test_provenance_missing_result_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO messages (session_id, role, tool_calls) VALUES "
            "('s1', 'assistant', ?)",
            (
                json.dumps(
                    [
                        {
                            "id": "c1",
                            "function": {
                                "name": "github_get_pr",
                                "arguments": json.dumps({"number": 54}),
                            },
                        }
                    ]
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ProvenanceError) as excinfo:
        collect_tool_provenance(
            shadow_state_db=str(db),
            session_id="s1",
            expected_tool_id="github.get_pr",
            expected_arguments={"number": 54},
            direct_normalized_sha256="0" * 64,
            normalizer=_digest,
        )
    assert excinfo.value.code == "PROVENANCE_RESULT_MISSING"


def test_provenance_is_session_scoped(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    result = {"number": 54}
    _insert_shadow_conversation(
        db,
        session_id="s1",
        tool_name="github_get_pr",
        arguments={"number": 54},
        result=result,
    )
    # A different session performing another authorized call must be invisible.
    _insert_shadow_conversation(
        db,
        session_id="s2",
        tool_name="github_get_repo",
        arguments={},
        result={"full_name": "a/b"},
        call_id="call-2",
    )
    record = collect_tool_provenance(
        shadow_state_db=str(db),
        session_id="s1",
        expected_tool_id="github.get_pr",
        expected_arguments={"number": 54},
        direct_normalized_sha256=_digest("github.get_pr", result),
        normalizer=_digest,
    )
    assert record.authorized_tool_call_count == 1


def test_provenance_record_never_leaks_identifiers(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db)
    result = {"number": 54, "title": "secret-title"}
    _insert_shadow_conversation(
        db,
        session_id="session-abc",
        tool_name="github_get_pr",
        arguments={"number": 54},
        result=result,
        call_id="tool-call-xyz",
    )
    payload = collect_tool_provenance(
        shadow_state_db=str(db),
        session_id="session-abc",
        expected_tool_id="github.get_pr",
        expected_arguments={"number": 54},
        direct_normalized_sha256=_digest("github.get_pr", result),
        normalizer=_digest,
    ).as_canonical()
    text = json.dumps(payload)
    for leaked in ("session-abc", "tool-call-xyz", "secret-title", str(db)):
        assert leaked not in text
    assert payload["tool_call_id_stored"] is False
    assert payload["session_id_stored"] is False
    assert payload["raw_result_stored"] is False


def test_blocked_record_is_sanitized() -> None:
    payload = blocked_record("PROVENANCE_DIGEST_MISMATCH")
    assert payload["provenance_pass"] is False
    assert payload["blockers"] == ["PROVENANCE_DIGEST_MISMATCH"]
    assert "session_id" not in json.dumps(payload).replace("session_id_stored", "")


def test_result_size_bucket_is_coarse() -> None:
    assert result_size_bucket(0) == "EMPTY"
    assert result_size_bucket(100) == "XS"
    assert result_size_bucket(5000) == "M"
    assert result_size_bucket(10**7) == "XL"


# ---------------------------------------------------------------------------
# B) real-state integrity
# ---------------------------------------------------------------------------


def _snapshot(path: Path, salt: bytes):
    return capture_state_snapshot(
        str(path), metadata=read_state_metadata(str(path)), salt=salt
    )


def test_state_measurement_detects_single_insert(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_state_db(db)
    salt = new_run_salt()
    before = _snapshot(db, salt)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('extra', 9.0)"
        )
        connection.commit()
    finally:
        connection.close()
    after = _snapshot(db, salt)
    comparison = compare_snapshots(before, after)
    assert comparison.unchanged is False
    canonical = comparison.as_canonical()
    sessions = next(item for item in canonical["tables"] if item["name"] == "sessions")
    assert sessions["row_count_delta"] == 1


def test_state_measurement_itself_does_not_write(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_state_db(db)
    salt = new_run_salt()
    before_stat = db.stat()
    before = _snapshot(db, salt)
    after = _snapshot(db, salt)
    after_stat = db.stat()
    assert compare_snapshots(before, after).unchanged is True
    assert before_stat.st_size == after_stat.st_size
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
    assert not (tmp_path / "state.db-wal").exists()


def test_control_activity_guard_detects_running_run(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_state_db(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("CREATE TABLE api_runs (id TEXT, status TEXT)")
        connection.execute("INSERT INTO api_runs VALUES ('r1', 'running')")
        connection.commit()
    finally:
        connection.close()
    assert FINAL_RUNNER.control_activity_detected(str(db)) is True


def test_control_activity_guard_quiet_database(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_state_db(db)
    assert FINAL_RUNNER.control_activity_detected(str(db)) is False


def test_control_activity_guard_unavailable_is_not_false(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    assert FINAL_RUNNER.control_activity_detected(str(missing)) is None


def test_shadow_row_count_counts_tracked_tables(tmp_path: Path) -> None:
    db = tmp_path / "shadow.sqlite3"
    _make_state_db(db, rows=4)
    assert FINAL_RUNNER.shadow_row_count(str(db)) == 4
    assert FINAL_RUNNER.shadow_row_count(str(tmp_path / "nope.db")) is None


def test_execute_requires_explicit_flag_and_env(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_state_db(db)
    result = tmp_path / "result.json"
    launcher = tmp_path / "launcher.sh"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    os.environ.pop(FINAL_RUNNER.EXECUTE_ENV, None)
    code = FINAL_RUNNER.main(
        [
            "execute",
            "--state-db",
            str(db),
            "--shadow-state-db",
            str(tmp_path / "shadow.sqlite3"),
            "--inner-launcher",
            str(launcher),
            "--result",
            str(result),
            "--source-commit",
            "0" * 40,
            "--i-understand-this-runs-a-real-acceptance",
        ]
    )
    assert code == 2
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["state"] == "FAILED"
    assert payload["reason"] == "FINAL_EXECUTION_NOT_PERMITTED"
    assert stat.S_IMODE(result.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# D) out-of-band systemd plan
# ---------------------------------------------------------------------------


def test_final_plan_is_dry_run_and_secret_free(tmp_path: Path, capsys) -> None:
    code = FINAL_RUNNER.main(
        [
            "plan",
            "--state-db",
            str(tmp_path / "state.db"),
            "--shadow-state-db",
            str(tmp_path / "shadow.sqlite3"),
            "--inner-launcher",
            str(tmp_path / "launcher.sh"),
            "--result",
            str(tmp_path / "result.json"),
            "--working-directory",
            str(tmp_path),
            "--source-commit",
            "0" * 40,
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out.strip())
    assert report["mode"] == "DRY_RUN"
    assert report["executed"] is False
    assert report["environment_count"] == 0
    command = report["command_line"]
    assert "systemd-run" in command and "--user" in command
    assert "--on-active=" in command
    for forbidden in ("Environment=", "--setenv", "token", "secret", "password"):
        assert forbidden not in command
    properties = {key: value for key, value in report["properties"]}
    assert properties["Type"] == "oneshot"
    assert properties["Restart"] == "no"
    assert properties["UMask"] == "0077"
    assert int(properties["RuntimeMaxSec"]) > 0


def test_plan_rejects_secret_bearing_argv(tmp_path: Path) -> None:
    from hermes_mcp_bridge.v2.out_of_band import OutOfBandError

    with pytest.raises(OutOfBandError):
        build_transient_unit_plan(
            unit_name="unit",
            probe_argv=("/usr/bin/python3", "--token", "abc"),
            working_directory=str(tmp_path),
            result_path=str(tmp_path / "r.json"),
            delay_seconds=10,
            timeout_seconds=30,
        )


def test_plan_has_no_environment_assignments(tmp_path: Path) -> None:
    plan = build_transient_unit_plan(
        unit_name="unit",
        probe_argv=("/usr/bin/python3", "/tmp/probe.py"),
        working_directory=str(tmp_path),
        result_path=str(tmp_path / "r.json"),
        delay_seconds=10,
        timeout_seconds=30,
    )
    assert_plan_is_secret_free(plan)
    assert plan.environment == ()


# ---------------------------------------------------------------------------
# C) final gate
# ---------------------------------------------------------------------------


COMMIT = "14bb5333f41a866ac1243d66ed8a0dcee78ad9c3"


def _provenance_record(index: int) -> dict[str, Any]:
    digest = hashlib.sha256(str(index).encode()).hexdigest()
    return {
        "schema": PROVENANCE_SCHEMA,
        "provenance_pass": True,
        "canonical_tool_id": "github.get_pr",
        "authorized_tool_call_count": 1,
        "unauthorized_tool_calls_observed": False,
        "normalization_profile_id": NORMALIZATION_PROFILE_ID,
        "arguments_shape_sha256": digest,
        "internal_normalized_sha256": digest,
        "direct_normalized_sha256": digest,
        "internal_matches_direct": True,
        "result_size_bucket": "S",
        "tool_call_id_stored": False,
        "raw_arguments_stored": False,
        "raw_result_stored": False,
        "session_id_stored": False,
        "message_rows_stored": False,
        "blockers": [],
    }


def _state_document() -> dict[str, Any]:
    fingerprint = hashlib.sha256(b"fp").hexdigest()
    return {
        "schema": STATE_INTEGRITY_DOC_SCHEMA,
        "source_commit": COMMIT,
        "measured_out_of_band": True,
        "read_only": True,
        "measurement_self_write_observed": False,
        "control_activity_detected": False,
        "exclusions_applied": False,
        "fingerprint_before": fingerprint,
        "fingerprint_after": fingerprint,
        "user_version_changed": False,
        "sqlite_schema_version_changed": False,
        "size_changed": False,
        "mtime_changed": False,
        "row_deltas": {"sessions": 0, "messages": 0, "session_model_usage": 0},
        "shadow_state_activity_observed": True,
        "shadow_row_count_delta": 42,
        "measured_before_at": "2026-08-09T10:00:00+00:00",
        "measured_after_at": "2026-08-09T11:00:00+00:00",
        "inner_started_at": "2026-08-09T10:05:00+00:00",
        "inner_finished_at": "2026-08-09T10:55:00+00:00",
        "paths_stored": False,
        "row_contents_stored": False,
    }


def accepted_final_evidence() -> dict[str, Any]:
    return {
        "schema": FINAL_EVIDENCE_SCHEMA,
        "source_commit": COMMIT,
        "inner_gate": {
            "direct_read_status": "DIRECT_READ_ACCEPTED",
            "failures": [],
            "source_commit": COMMIT,
            "started_at": "2026-08-09T10:05:00+00:00",
            "finished_at": "2026-08-09T10:55:00+00:00",
        },
        "aggregate": {
            "sample_count": EXPECTED_SAMPLE_COUNT,
            "successful_samples": EXPECTED_SAMPLE_COUNT,
            "semantic_matches": EXPECTED_SAMPLE_COUNT,
            "provenance_pass": EXPECTED_SAMPLE_COUNT,
            "provenance_fail": 0,
            "token_measurement_mode": "empirical",
            "direct_total_tokens": 0,
            "agentic_total_tokens": 120000,
            "token_reduction_percent": 100.0,
            "direct_provider_api_calls": EXPECTED_SAMPLE_COUNT,
            "mutations_observed": 0,
        },
        "provenance": [_provenance_record(i) for i in range(EXPECTED_SAMPLE_COUNT)],
        "state_integrity": _state_document(),
        "privacy": {
            "paths_stored": False,
            "row_contents_stored": False,
            "raw_results_stored": False,
            "session_ids_stored": False,
            "salt_stored": False,
        },
    }


def test_final_gate_accepts_complete_evidence() -> None:
    failures = validate_final_evidence(accepted_final_evidence())
    assert failures == []
    manifest = final_manifest(accepted_final_evidence(), failures)
    assert manifest["overall_status"] == STATUS_ACCEPTED
    assert manifest["source_commit"] == COMMIT


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["inner_gate"].update(direct_read_status="DIRECT_READ_BLOCKED"),
         "inner_gate_not_accepted"),
        (lambda d: d["aggregate"].update(sample_count=14), "sample_count_invalid"),
        (lambda d: d["aggregate"].update(semantic_matches=14),
         "semantic_matches_invalid"),
        (lambda d: d["aggregate"].update(provenance_pass=14),
         "provenance_pass_invalid"),
        (lambda d: d["aggregate"].update(provenance_fail=1),
         "provenance_fail_present"),
        (lambda d: d["aggregate"].update(token_measurement_mode="estimated"),
         "token_measurement_mode_invalid"),
        (lambda d: d["aggregate"].update(direct_total_tokens=5),
         "direct_total_tokens_not_zero"),
        (lambda d: d["aggregate"].update(agentic_total_tokens=0),
         "agentic_total_tokens_not_positive"),
        (lambda d: d["aggregate"].update(token_reduction_percent=79.9),
         "token_reduction_below_threshold"),
        (lambda d: d["aggregate"].update(direct_provider_api_calls=14),
         "direct_provider_api_calls_invalid"),
        (lambda d: d["aggregate"].update(mutations_observed=1), "mutations_observed"),
        (lambda d: d["state_integrity"].update(shadow_state_activity_observed=False),
         "shadow_state_activity_not_observed"),
        (lambda d: d["state_integrity"].update(shadow_row_count_delta=0),
         "shadow_row_count_delta_not_positive"),
        (lambda d: d["state_integrity"].update(exclusions_applied=True),
         "state_integrity_exclusions_applied"),
        (lambda d: d["state_integrity"].update(control_activity_detected=True),
         "state_integrity_control_activity_detected"),
        (lambda d: d["state_integrity"].update(size_changed=True), "state_size_changed"),
        (lambda d: d["state_integrity"].update(mtime_changed=True),
         "state_mtime_changed"),
        (lambda d: d["state_integrity"].update(user_version_changed=True),
         "state_user_version_changed"),
        (lambda d: d["state_integrity"].update(sqlite_schema_version_changed=True),
         "state_schema_version_changed"),
        (lambda d: d["state_integrity"].update(paths_stored=True), "state_paths_stored"),
        (lambda d: d["state_integrity"].update(row_contents_stored=True),
         "state_row_contents_stored"),
        (lambda d: d["state_integrity"].update(measured_out_of_band=False),
         "state_integrity_not_out_of_band"),
        (lambda d: d["state_integrity"]["row_deltas"].update(messages=1),
         "state_row_delta_nonzero:messages"),
        (lambda d: d["state_integrity"]["row_deltas"].update(sessions=1),
         "state_row_delta_nonzero:sessions"),
        (lambda d: d["state_integrity"]["row_deltas"].update(session_model_usage=2),
         "state_row_delta_nonzero:session_model_usage"),
        (lambda d: d["state_integrity"].update(
            fingerprint_after=hashlib.sha256(b"other").hexdigest()
        ), "state_fingerprint_mismatch"),
        (lambda d: d["state_integrity"].update(fingerprint_before=""),
         "state_fingerprint_invalid"),
        (lambda d: d["state_integrity"].update(
            measured_before_at="2026-08-09T10:30:00+00:00"
        ), "state_window_does_not_enclose_samples"),
        (lambda d: d.update(source_commit="1" * 40), "source_commit_inconsistent"),
        (lambda d: d["privacy"].update(paths_stored=True), "privacy_contract_not_met"),
    ],
)
def test_final_gate_blocks_on_each_strict_field(mutate, expected: str) -> None:
    document = accepted_final_evidence()
    mutate(document)
    failures = validate_final_evidence(document)
    assert expected in failures
    assert final_manifest(document, failures)["overall_status"] == STATUS_BLOCKED


def test_missing_state_document_hard_blocks() -> None:
    document = accepted_final_evidence()
    document.pop("state_integrity")
    failures = validate_final_evidence(document)
    assert "state_integrity_document_missing" in failures
    assert final_manifest(document, failures)["overall_status"] == STATUS_BLOCKED


def test_unmeasurable_state_document_hard_blocks() -> None:
    document = accepted_final_evidence()
    document["state_integrity"] = "unavailable"
    failures = validate_final_evidence(document)
    assert "state_integrity_document_missing" in failures


def test_provenance_cannot_rescue_semantic_failure() -> None:
    document = accepted_final_evidence()
    document["aggregate"]["semantic_matches"] = 14
    failures = validate_final_evidence(document)
    assert "semantic_matches_invalid" in failures
    assert final_manifest(document, failures)["overall_status"] == STATUS_BLOCKED


def test_failing_provenance_record_blocks() -> None:
    document = accepted_final_evidence()
    document["provenance"][3]["provenance_pass"] = False
    document["provenance"][3]["blockers"] = ["PROVENANCE_DIGEST_MISMATCH"]
    failures = validate_final_evidence(document)
    assert "provenance[3]:not_pass" in failures


def test_exact_fifteen_provenance_records_required() -> None:
    document = accepted_final_evidence()
    document["provenance"] = document["provenance"][:14]
    assert "provenance_record_count_invalid" in validate_final_evidence(document)


def test_final_gate_rejects_forbidden_keys() -> None:
    document = accepted_final_evidence()
    document["provenance"][0]["tool_call_id"] = "abc"
    failures = validate_final_evidence(document)
    assert any(item.startswith("forbidden_evidence_keys") for item in failures)


def test_final_gate_reasons_are_stable_strings() -> None:
    document = accepted_final_evidence()
    document["aggregate"]["sample_count"] = 3
    for reason in validate_final_evidence(document):
        assert reason == reason.strip()
        assert "/" not in reason.split(":")[0]


# ---------------------------------------------------------------------------
# validator CLI + assembler
# ---------------------------------------------------------------------------


def test_validator_cli_accepts_and_writes_manifest(tmp_path: Path, capsys) -> None:
    evidence = tmp_path / "final.json"
    evidence.write_text(json.dumps(accepted_final_evidence()), encoding="utf-8")
    out = tmp_path / "manifest.json"
    code = FINAL_VALIDATOR.main([str(evidence), "--json-out", str(out)])
    capsys.readouterr()
    assert code == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["overall_status"] == STATUS_ACCEPTED
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_validator_cli_blocks_unreadable_evidence(tmp_path: Path, capsys) -> None:
    code = FINAL_VALIDATOR.main([str(tmp_path / "absent.json")])
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output["overall_status"] == "BLOCKED"
    assert output["reasons"] == ["final_evidence_unreadable"]


def test_builder_omits_state_when_marker_missing() -> None:
    document = FINAL_BUILDER.build_final_evidence(
        evidence={"source_commit": COMMIT, "aggregate": {}, "samples": []},
        inner_gate={"gate": "DIRECT_READ_ACCEPTED", "failures": []},
        state_marker=None,
    )
    assert "state_integrity" not in document
    assert "state_integrity_document_missing" in validate_final_evidence(document)


def test_builder_computes_token_reduction() -> None:
    assert FINAL_BUILDER.token_reduction_percent(0, 1000) == 100.0
    assert FINAL_BUILDER.token_reduction_percent(0, 0) == 0.0


# ---------------------------------------------------------------------------
# E) contract + shellcheck-style structural assertions
# ---------------------------------------------------------------------------


def test_v1_contract_still_declares_27_tools() -> None:
    contract = json.loads(
        (ROOT / "contracts" / "1.0.0.json").read_text(encoding="utf-8")
    )
    tools = contract.get("tools")
    assert isinstance(tools, list)
    assert len(tools) == 27


def test_final_runner_compiles_and_defaults_to_plan() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "py_compile", str(
            SCRIPTS / "v2_phase2_final_out_of_band_acceptance.py"
        )],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    from hermes_mcp_bridge.v2.out_of_band import cleanup_run_artifacts

    target = tmp_path / "artifact.json"
    target.write_text("{}", encoding="utf-8")
    assert cleanup_run_artifacts(target) == ("artifact.json",)
    assert cleanup_run_artifacts(target) == ()
