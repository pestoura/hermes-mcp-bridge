"""Hermetic tests for the Phase 2 final OOB runner blocker repairs.

Covers:

A) the private shadow-activity witness handoff (sanitized, single-consumption,
   written before the inner launcher's cleanup, no CLI fabrication path);
B) the transient systemd sandbox (ProtectHome stays read-only, explicit and
   minimal ReadWritePaths, read-only credential source, no secret in
   argv/env/unit properties);
C) the Hermes 0.20 control-activity guard (QUIET / ACTIVE / UNMEASURABLE with
   fail-closed schema introspection);
D) the background-writer precondition.

Nothing here starts, stops or touches a real gateway or a real state database.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hermes_mcp_bridge.v2.control_activity import (  # noqa: E402
    REQUIRED_TABLES,
    STATUS_ACTIVE,
    STATUS_QUIET,
    STATUS_UNMEASURABLE,
    evaluate_control_activity,
)
from hermes_mcp_bridge.v2.out_of_band import (  # noqa: E402
    OutOfBandError,
    assert_plan_is_secret_free,
    assert_sandbox_is_minimal,
    build_transient_unit_plan,
    read_only_paths,
    writable_paths,
)
from hermes_mcp_bridge.v2.shadow_witness import (  # noqa: E402
    SHADOW_WITNESS_SCHEMA,
    WITNESS_ENV,
    ShadowCounts,
    build_witness,
    consume_witness,
    read_shadow_counts,
    write_witness,
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
WITNESS_CLI = _load_script("v2_phase2_shadow_witness")

LAUNCHER = SCRIPTS / "v2_phase2_connected_jarvas.sh"
COMMIT = "340f799611aa36ba0122ac177db31f6e00473e1a"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _make_hermes_020_state_db(path: Path) -> None:
    """Create the subset of the real Hermes 0.20 schema the guard needs."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                started_at REAL,
                ended_at REAL,
                last_activity_at REAL,
                archived INTEGER DEFAULT 0,
                expiry_finalized INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT
            );
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE async_delegations (
                delegation_id TEXT PRIMARY KEY,
                state TEXT,
                delivery_state TEXT,
                dispatched_at REAL
            );
            CREATE TABLE delivery_obligations (
                obligation_id TEXT PRIMARY KEY,
                state TEXT,
                created_at REAL
            );
            CREATE TABLE compression_locks (
                session_id TEXT PRIMARY KEY,
                holder TEXT,
                acquired_at REAL,
                expires_at REAL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def _shadow_db(path: Path, *, sessions: int, usage: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY);
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL, model TEXT NOT NULL
            );
            """
        )
        for index in range(sessions):
            connection.execute("INSERT INTO sessions VALUES (?)", (f"s{index}",))
        for index in range(usage):
            connection.execute(
                "INSERT INTO session_model_usage VALUES (?, 'm')", (f"s{index}",)
            )
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# C) control-activity guard against the real Hermes 0.20 schema
# ---------------------------------------------------------------------------


def test_guard_quiet_on_idle_hermes_020_schema(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_hermes_020_state_db(db)
    report = evaluate_control_activity(str(db), now=1_000_000.0)
    assert report.status == STATUS_QUIET
    assert report.quiet is True
    assert report.blockers == ()
    payload = report.as_canonical()
    assert payload["row_contents_read"] is False
    assert payload["identifiers_read"] is False


def test_guard_active_on_api_run_heartbeat(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_hermes_020_state_db(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO sessions (id, last_activity_at) VALUES ('live', ?)",
            (999_999.0,),
        )
        connection.commit()
    finally:
        connection.close()
    report = evaluate_control_activity(str(db), now=1_000_000.0)
    assert report.status == STATUS_ACTIVE
    assert report.as_canonical()["recently_active_sessions"] == 1


def test_runner_boolean_view_reports_active(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_hermes_020_state_db(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO sessions (id, last_activity_at) VALUES ('live', ?)",
            (time.time(),),
        )
        connection.commit()
    finally:
        connection.close()
    assert FINAL_RUNNER.control_activity_detected(str(db)) is True


def test_guard_active_on_async_delegation(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_hermes_020_state_db(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO async_delegations "
            "(delegation_id, state, delivery_state) VALUES ('d1', 'running', 'pending')"
        )
        connection.commit()
    finally:
        connection.close()
    report = evaluate_control_activity(str(db), now=1_000_000.0)
    assert report.status == STATUS_ACTIVE
    payload = report.as_canonical()
    assert payload["active_delegations"] == 1
    assert payload["pending_deliveries"] == 1


def test_guard_active_on_pending_obligation(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_hermes_020_state_db(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO delivery_obligations (obligation_id, state) "
            "VALUES ('o1', 'pending')"
        )
        connection.commit()
    finally:
        connection.close()
    assert evaluate_control_activity(str(db), now=1_000_000.0).status == STATUS_ACTIVE


def test_guard_unmeasurable_when_async_delegations_missing(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_hermes_020_state_db(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("DROP TABLE async_delegations")
        connection.commit()
    finally:
        connection.close()
    report = evaluate_control_activity(str(db), now=1_000_000.0)
    assert report.status == STATUS_UNMEASURABLE
    assert report.blockers == ("CONTROL_SCHEMA_TABLE_MISSING",)
    # UNMEASURABLE must never collapse into "quiet".
    assert report.quiet is False
    assert FINAL_RUNNER.control_activity_detected(str(db)) is None


def test_guard_unmeasurable_on_malformed_schema(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_hermes_020_state_db(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("DROP TABLE async_delegations")
        connection.execute("CREATE TABLE async_delegations (delegation_id TEXT)")
        connection.commit()
    finally:
        connection.close()
    report = evaluate_control_activity(str(db), now=1_000_000.0)
    assert report.status == STATUS_UNMEASURABLE
    assert report.blockers == ("CONTROL_SCHEMA_COLUMN_MISSING",)


def test_guard_unmeasurable_when_database_absent(tmp_path: Path) -> None:
    report = evaluate_control_activity(str(tmp_path / "absent.db"))
    assert report.status == STATUS_UNMEASURABLE
    assert report.blockers == ("CONTROL_DB_UNREADABLE",)


def test_guard_tracks_async_delegations_explicitly() -> None:
    assert "async_delegations" in REQUIRED_TABLES
    assert "state" in REQUIRED_TABLES["async_delegations"]


def test_guard_report_carries_no_identifier(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_hermes_020_state_db(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO sessions (id, last_activity_at) VALUES ('secret-session', ?)",
            (999_999.0,),
        )
        connection.commit()
    finally:
        connection.close()
    text = json.dumps(evaluate_control_activity(str(db), now=1_000_000.0).as_canonical())
    assert "secret-session" not in text
    assert str(db) not in text


def test_guard_does_not_write_to_the_database(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_hermes_020_state_db(db)
    before = db.stat()
    evaluate_control_activity(str(db), now=1_000_000.0)
    after = db.stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    assert not (tmp_path / "state.db-wal").exists()


# ---------------------------------------------------------------------------
# A) shadow activity witness handoff
# ---------------------------------------------------------------------------


def _witness_document(tmp_path: Path, *, commit: str = COMMIT) -> dict[str, Any]:
    shadow_home = tmp_path / "shadow-home"
    (shadow_home).mkdir()
    shadow_db = shadow_home / "state.db"
    _shadow_db(shadow_db, sessions=3, usage=2)
    source_db = tmp_path / "real-state.db"
    _make_hermes_020_state_db(source_db)
    return build_witness(
        source_commit=commit,
        before=ShadowCounts(sessions=0, session_model_usage=0),
        after=read_shadow_counts(str(shadow_db)),
        shadow_state_db=str(shadow_db),
        source_state_db=str(source_db),
        shadow_home=str(shadow_home),
        handoff_path=str(tmp_path / "witness.json"),
    )


def test_witness_proves_positive_growth_and_disposability(tmp_path: Path) -> None:
    document = _witness_document(tmp_path)
    assert document["schema"] == SHADOW_WITNESS_SCHEMA
    assert document["sessions_row_delta"] == 3
    assert document["session_model_usage_row_delta"] == 2
    assert document["sessions_growth_positive"] is True
    assert document["session_model_usage_growth_positive"] is True
    assert document["shadow_activity_observed"] is True
    assert document["shadow_db_distinct_from_source"] is True
    assert document["shadow_db_inside_disposable_home"] is True
    assert document["handoff_outside_shadow_home"] is True


def test_witness_contains_no_paths_or_identifiers(tmp_path: Path) -> None:
    document = _witness_document(tmp_path)
    text = json.dumps(document)
    assert str(tmp_path) not in text
    for key, value in document.items():
        if key in ("schema", "source_commit"):
            continue
        assert not isinstance(value, str), key


def test_witness_file_is_0600_and_consumed_once(tmp_path: Path) -> None:
    document = _witness_document(tmp_path)
    handoff = tmp_path / "witness.json"
    write_witness(handoff, document)
    assert stat.S_IMODE(handoff.stat().st_mode) == 0o600
    first = consume_witness(handoff, expected_commit=COMMIT)
    assert first is not None
    assert handoff.exists() is False
    assert consume_witness(handoff, expected_commit=COMMIT) is None


def test_witness_rejected_on_commit_mismatch(tmp_path: Path) -> None:
    document = _witness_document(tmp_path)
    handoff = tmp_path / "witness.json"
    write_witness(handoff, document)
    assert consume_witness(handoff, expected_commit="1" * 40) is None
    # Rejected witnesses are still removed, so they cannot be replayed.
    assert handoff.exists() is False


def test_witness_rejected_on_schema_mismatch(tmp_path: Path) -> None:
    document = _witness_document(tmp_path)
    document["schema"] = "something-else/1"
    handoff = tmp_path / "witness.json"
    handoff.write_text(json.dumps(document), encoding="utf-8")
    assert consume_witness(handoff, expected_commit=COMMIT) is None


def test_witness_rejected_when_growth_is_not_positive(tmp_path: Path) -> None:
    shadow_home = tmp_path / "shadow-home"
    shadow_home.mkdir()
    shadow_db = shadow_home / "state.db"
    _shadow_db(shadow_db, sessions=0, usage=0)
    source_db = tmp_path / "real-state.db"
    _make_hermes_020_state_db(source_db)
    document = build_witness(
        source_commit=COMMIT,
        before=ShadowCounts(sessions=0, session_model_usage=0),
        after=read_shadow_counts(str(shadow_db)),
        shadow_state_db=str(shadow_db),
        source_state_db=str(source_db),
        shadow_home=str(shadow_home),
        handoff_path=str(tmp_path / "witness.json"),
    )
    assert document["shadow_activity_observed"] is False
    handoff = tmp_path / "witness.json"
    write_witness(handoff, document)
    assert consume_witness(handoff, expected_commit=COMMIT) is None


def test_witness_cli_capture_and_emit_roundtrip(tmp_path: Path, capsys) -> None:
    shadow_home = tmp_path / "shadow-home"
    shadow_home.mkdir()
    shadow_db = shadow_home / "state.db"
    _shadow_db(shadow_db, sessions=1, usage=1)
    source_db = tmp_path / "real-state.db"
    _make_hermes_020_state_db(source_db)
    baseline = tmp_path / "baseline.json"
    handoff = tmp_path / "witness.json"

    assert (
        WITNESS_CLI.main(
            [
                "capture",
                "--shadow-state-db",
                str(shadow_db),
                "--baseline",
                str(baseline),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert stat.S_IMODE(baseline.stat().st_mode) == 0o600

    _shadow_db  # noqa: B018 - readability anchor
    connection = sqlite3.connect(shadow_db)
    try:
        connection.execute("INSERT INTO sessions VALUES ('later')")
        connection.execute("INSERT INTO session_model_usage VALUES ('later', 'm')")
        connection.commit()
    finally:
        connection.close()

    assert (
        WITNESS_CLI.main(
            [
                "emit",
                "--shadow-state-db",
                str(shadow_db),
                "--source-state-db",
                str(source_db),
                "--shadow-home",
                str(shadow_home),
                "--baseline",
                str(baseline),
                "--handoff",
                str(handoff),
                "--source-commit",
                COMMIT,
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out.strip())
    assert output == {"status": "SHADOW_WITNESS_WRITTEN"}
    consumed = consume_witness(handoff, expected_commit=COMMIT)
    assert consumed is not None
    assert consumed["sessions_row_delta"] == 1
    assert consumed["session_model_usage_row_delta"] == 1


def test_launcher_emits_witness_before_cleanup_without_disabling_it() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "HERMES_V2_FINAL_SHADOW_WITNESS_FILE" in text
    assert "emit_shadow_witness" in text
    # cleanup itself is unchanged: the shadow home is still destroyed.
    assert 'rm -rf -- "$SHADOW_HOME"' in text
    # and the witness is written BEFORE that destruction.
    cleanup_body = text[text.index("cleanup() {") : text.index("trap cleanup EXIT")]
    assert cleanup_body.index("emit_shadow_witness") < cleanup_body.index(
        'rm -rf -- "$SHADOW_HOME"'
    )
    assert 'blocked "SHADOW_WITNESS_PATH_INVALID"' in text
    assert 'SHADOW_WITNESS_FILE" != "$SHADOW_HOME"' in text


def test_launcher_handoff_is_inert_without_the_env_variable() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    assert 'SHADOW_WITNESS_ENABLED=0' in text
    assert '[[ "$SHADOW_WITNESS_ENABLED" == \'1\' ]] || return 0' in text


def test_launcher_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(LAUNCHER)], check=True)


def test_runner_has_no_caller_supplied_positive_control() -> None:
    source = (SCRIPTS / "v2_phase2_final_out_of_band_acceptance.py").read_text(
        encoding="utf-8"
    )
    assert "--shadow-row-count-after" not in source
    assert "shadow_row_count_after" not in source


# ---------------------------------------------------------------------------
# B) systemd sandbox
# ---------------------------------------------------------------------------


def _plan(tmp_path: Path, **kwargs: Any):
    defaults: dict[str, Any] = dict(
        unit_name="hermes-v2-final-oob-acceptance",
        probe_argv=("/usr/bin/python3", "/opt/probe.py"),
        working_directory=str(tmp_path / "work"),
        result_path=str(tmp_path / "out" / "result.json"),
        delay_seconds=120,
        timeout_seconds=900,
    )
    defaults.update(kwargs)
    return build_transient_unit_plan(**defaults)


def test_sandbox_grants_only_the_named_acceptance_directories(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        writable_paths=(str(tmp_path / "work"), str(tmp_path / "out")),
        read_only_paths=(str(tmp_path / "creds"),),
    )
    properties = plan.property_map()
    assert properties["ProtectSystem"] == "strict"
    assert properties["ProtectHome"] == "read-only"
    assert properties["NoNewPrivileges"] == "yes"
    assert set(writable_paths(plan)) == {str(tmp_path / "work"), str(tmp_path / "out")}
    assert read_only_paths(plan) == (str(tmp_path / "creds"),)
    assert_sandbox_is_minimal(plan)


def test_sandbox_never_grants_write_to_arbitrary_home(tmp_path: Path) -> None:
    plan = _plan(tmp_path, writable_paths=(str(tmp_path / "work"),))
    home = os.environ.get("HOME") or str(Path.home())
    grants = writable_paths(plan)
    assert home not in grants
    for grant in grants:
        assert grant != home
        assert grant not in ("/", "/home", "/etc", "/var", "/root")


def test_sandbox_rejects_home_root_as_writable(tmp_path: Path) -> None:
    home = os.environ.get("HOME") or str(Path.home())
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, writable_paths=(home,))
    assert excinfo.value.code == "OOB_SANDBOX_PATH_TOO_BROAD"


def test_sandbox_rejects_filesystem_root_as_writable(tmp_path: Path) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, writable_paths=("/",))
    assert excinfo.value.code == "OOB_SANDBOX_PATH_TOO_BROAD"


def test_sandbox_rejects_relative_grant(tmp_path: Path) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, writable_paths=("relative/dir",))
    assert excinfo.value.code == "OOB_PATH_NOT_ABSOLUTE"


def test_sandbox_rejects_same_path_both_writable_and_read_only(tmp_path: Path) -> None:
    shared = str(tmp_path / "work")
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, writable_paths=(shared,), read_only_paths=(shared,))
    assert excinfo.value.code == "OOB_SANDBOX_PATH_CONFLICT"


def test_sandbox_keeps_auth_source_mounted_read_only(tmp_path: Path) -> None:
    creds = str(tmp_path / "hermes-home")
    plan = _plan(
        tmp_path, writable_paths=(str(tmp_path / "work"),), read_only_paths=(creds,)
    )
    assert creds in read_only_paths(plan)
    assert creds not in writable_paths(plan)


def test_sandbox_carries_no_sensitive_token_anywhere(tmp_path: Path) -> None:
    plan = _plan(tmp_path, writable_paths=(str(tmp_path / "work"),))
    assert_plan_is_secret_free(plan)
    assert plan.environment == ()
    rendered = plan.command_line()
    for forbidden in ("Environment=", "--setenv", "token", "secret", "password"):
        assert forbidden not in rendered
    for _, value in plan.properties:
        assert "token" not in value.lower()


def test_sandbox_rejects_sensitive_bearing_grant(tmp_path: Path) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, writable_paths=("/srv/api-token-store",))
    assert excinfo.value.code == "OOB_SECRET_BEARING_ARGUMENT"


def test_runner_plan_emits_minimal_writable_grants(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    code = FINAL_RUNNER.main(
        [
            "plan",
            "--state-db",
            str(hermes_home / "state.db"),
            "--shadow-state-db",
            str(tmp_path / "shadow.sqlite3"),
            "--inner-launcher",
            str(tmp_path / "launcher.sh"),
            "--result",
            str(tmp_path / "results" / "result.json"),
            "--working-directory",
            str(tmp_path / "work"),
            "--source-commit",
            COMMIT,
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out.strip())
    properties = dict(tuple(item) for item in report["properties"])
    assert properties["ProtectHome"] == "read-only"
    assert properties["ProtectSystem"] == "strict"
    grants = [value for key, value in report["properties"] if key == "ReadWritePaths"]
    assert sorted(grants) == sorted(
        [str(tmp_path / "work"), str(tmp_path / "results")]
    )
    read_only = [value for key, value in report["properties"] if key == "ReadOnlyPaths"]
    assert read_only == [str(hermes_home)]
    assert report["environment_count"] == 0
    assert report["background_writer_precondition"] == (
        "FINAL_BACKGROUND_WRITER_UNCONTROLLED"
    )


# ---------------------------------------------------------------------------
# D) background-writer precondition
# ---------------------------------------------------------------------------


def _execute_argv(tmp_path: Path, launcher: Path, *extra: str) -> list[str]:
    return [
        "execute",
        "--state-db",
        str(tmp_path / "state.db"),
        "--shadow-state-db",
        str(tmp_path / "shadow.sqlite3"),
        "--inner-launcher",
        str(launcher),
        "--result",
        str(tmp_path / "result.json"),
        "--shadow-witness",
        str(tmp_path / "witness.json"),
        "--source-commit",
        COMMIT,
        "--i-understand-this-runs-a-real-acceptance",
        *extra,
    ]


def test_execute_aborts_when_background_writer_uncontrolled(
    tmp_path: Path, monkeypatch
) -> None:
    _make_hermes_020_state_db(tmp_path / "state.db")
    launcher = tmp_path / "launcher.sh"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    monkeypatch.setenv(FINAL_RUNNER.EXECUTE_ENV, FINAL_RUNNER.EXECUTE_ENV_VALUE)
    code = FINAL_RUNNER.main(_execute_argv(tmp_path, launcher))
    assert code == 2
    payload = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert payload["reason"] == "FINAL_BACKGROUND_WRITER_UNCONTROLLED"


def test_execute_blocks_when_guard_is_unmeasurable(tmp_path: Path, monkeypatch) -> None:
    launcher = tmp_path / "launcher.sh"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    monkeypatch.setenv(FINAL_RUNNER.EXECUTE_ENV, FINAL_RUNNER.EXECUTE_ENV_VALUE)
    code = FINAL_RUNNER.main(
        _execute_argv(tmp_path, launcher, "--background-writer-controlled")
    )
    assert code == 2
    payload = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert payload["reason"] == "FINAL_CONTROL_GUARD_UNAVAILABLE"


def test_execute_blocks_without_a_valid_witness(tmp_path: Path, monkeypatch) -> None:
    _make_hermes_020_state_db(tmp_path / "state.db")
    launcher = tmp_path / "launcher.sh"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    monkeypatch.setenv(FINAL_RUNNER.EXECUTE_ENV, FINAL_RUNNER.EXECUTE_ENV_VALUE)
    code = FINAL_RUNNER.main(
        _execute_argv(tmp_path, launcher, "--background-writer-controlled")
    )
    assert code == 2
    payload = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert payload["reason"] == "FINAL_SHADOW_WITNESS_INVALID"


def test_execute_passes_handoff_path_to_the_inner_launcher(
    tmp_path: Path, monkeypatch
) -> None:
    _make_hermes_020_state_db(tmp_path / "state.db")
    seen = tmp_path / "seen-env.txt"
    launcher = tmp_path / "launcher.sh"
    launcher.write_text(
        "#!/bin/sh\n"
        f'printf "%s" "${WITNESS_ENV}" > {seen}\n'
        "exit 0\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    monkeypatch.setenv(FINAL_RUNNER.EXECUTE_ENV, FINAL_RUNNER.EXECUTE_ENV_VALUE)
    FINAL_RUNNER.main(
        _execute_argv(tmp_path, launcher, "--background-writer-controlled")
    )
    assert seen.read_text(encoding="utf-8") == str(tmp_path / "witness.json")


def test_zero_absolute_delta_is_not_relaxed() -> None:
    from hermes_mcp_bridge.v2.final_gate import (
        REQUIRED_ZERO_DELTA_TABLES,
        validate_final_evidence,
    )

    assert REQUIRED_ZERO_DELTA_TABLES == (
        "sessions",
        "messages",
        "session_model_usage",
    )
    document = {
        "state_integrity": {
            "row_deltas": {"sessions": 1, "messages": 0, "session_model_usage": 0}
        }
    }
    assert "state_row_delta_nonzero:sessions" in validate_final_evidence(document)
