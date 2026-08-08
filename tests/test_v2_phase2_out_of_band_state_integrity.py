"""Hermetic tests for the V2 Phase 2 out-of-band state-integrity foundation.

Nothing here schedules a real transient unit, contacts a provider, or touches a
live Hermes state database: every measurement runs against a temporary SQLite
fixture and every orchestration test runs in dry-run mode.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from hermes_mcp_bridge.v2.out_of_band import (
    MARKER_COMPLETED,
    MARKER_FAILED,
    OUT_OF_BAND_SCHEMA,
    OutOfBandError,
    TransientUnitPlan,
    assert_plan_is_secret_free,
    build_transient_unit_plan,
    cleanup_run_artifacts,
    dry_run_report,
    read_terminal_marker,
    schedule_transient_unit,
    write_terminal_marker,
)
from hermes_mcp_bridge.v2.state_integrity import (
    MIN_SALT_BYTES,
    STATE_SNAPSHOT_SCHEMA,
    TRACKED_TABLES,
    StateFileMetadata,
    StateIntegrityError,
    assert_state_paths_disjoint,
    capture_state_snapshot,
    compare_snapshots,
    new_run_salt,
    read_state_metadata,
)

_MODULE_DIR = Path(__file__).resolve().parent.parent
_STATE_MODULE = _MODULE_DIR / "src" / "hermes_mcp_bridge" / "v2" / "state_integrity.py"
_OOB_MODULE = _MODULE_DIR / "src" / "hermes_mcp_bridge" / "v2" / "out_of_band.py"
_PLANNER = _MODULE_DIR / "scripts" / "v2_phase2_out_of_band_state_integrity.py"
_WRAPPER = _MODULE_DIR / "scripts" / "v2_phase2_out_of_band_state_integrity.sh"


def _make_state_db(path: Path, *, sessions: int = 2, usage: bool = True) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
        if usage:
            connection.execute(
                "CREATE TABLE session_model_usage ("
                "id INTEGER PRIMARY KEY, tokens INTEGER NOT NULL)"
            )
        for index in range(sessions):
            connection.execute(
                "INSERT INTO sessions (name) VALUES (?)", (f"session-{index}",)
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _snapshot(path: Path, salt: bytes):
    return capture_state_snapshot(
        str(path), metadata=read_state_metadata(str(path)), salt=salt
    )


def _digest_of_file(path: Path) -> tuple[bytes, int, int]:
    info = os.stat(path)
    return path.read_bytes(), info.st_size, info.st_mtime_ns


# --------------------------------------------------------------------------
# 1. Measurement itself causes zero database change
# --------------------------------------------------------------------------


def test_measurement_causes_zero_database_change(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    before_bytes, before_size, before_mtime = _digest_of_file(db)

    salt = new_run_salt()
    first = _snapshot(db, salt)
    second = _snapshot(db, salt)
    comparison = compare_snapshots(first, second)

    after_bytes, after_size, after_mtime = _digest_of_file(db)
    assert after_bytes == before_bytes
    assert (after_size, after_mtime) == (before_size, before_mtime)
    assert comparison.unchanged is True
    assert comparison.digest_equal is True
    assert comparison.size_delta == 0
    assert all(item.row_count_delta == 0 for item in comparison.tables)
    # No sidecar journal/WAL file may be created by a read-only measurement.
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(db) + suffix).exists()


def test_connection_is_query_only(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    uri = f"file:{db.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO sessions (name) VALUES ('x')")
    finally:
        connection.close()


def test_module_uses_read_only_uri_and_query_only_pragma() -> None:
    source = _STATE_MODULE.read_text(encoding="utf-8")
    assert "?mode=ro" in source
    assert "PRAGMA query_only = ON" in source


# --------------------------------------------------------------------------
# 2. Real changes are detected
# --------------------------------------------------------------------------


def test_single_row_insert_is_detected(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    salt = new_run_salt()
    before = _snapshot(db, salt)

    connection = sqlite3.connect(db)
    try:
        connection.execute("INSERT INTO sessions (name) VALUES ('extra')")
        connection.commit()
    finally:
        connection.close()

    after = _snapshot(db, salt)
    comparison = compare_snapshots(before, after)
    assert comparison.unchanged is False
    assert comparison.digest_equal is False
    sessions = next(item for item in comparison.tables if item.name == "sessions")
    assert sessions.row_count_delta == 1
    assert sessions.max_rowid_delta == 1
    messages = next(item for item in comparison.tables if item.name == "messages")
    assert messages.row_count_delta == 0


def test_schema_change_is_detected(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    salt = new_run_salt()
    before = _snapshot(db, salt)

    connection = sqlite3.connect(db)
    try:
        connection.execute("ALTER TABLE messages ADD COLUMN extra TEXT")
        connection.commit()
    finally:
        connection.close()

    after = _snapshot(db, salt)
    comparison = compare_snapshots(before, after)
    messages = next(item for item in comparison.tables if item.name == "messages")
    assert messages.schema_changed is True
    assert comparison.sqlite_schema_version_changed is True
    assert comparison.unchanged is False


def test_missing_optional_table_is_recorded_not_inferred(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db", usage=False)
    snapshot = _snapshot(db, new_run_salt())
    usage = snapshot.table("session_model_usage")
    assert usage.present is False
    assert usage.row_count is None
    assert usage.max_rowid is None
    assert usage.schema_digest is None
    assert tuple(item.name for item in snapshot.tables) == TRACKED_TABLES


def test_table_presence_change_is_detected(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db", usage=False)
    salt = new_run_salt()
    before = _snapshot(db, salt)
    connection = sqlite3.connect(db)
    try:
        connection.execute("CREATE TABLE session_model_usage (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    after = _snapshot(db, salt)
    comparison = compare_snapshots(before, after)
    usage = next(
        item for item in comparison.tables if item.name == "session_model_usage"
    )
    assert usage.presence_changed is True
    assert usage.changed is True


def test_size_and_mtime_changes_are_detected(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    salt = new_run_salt()
    real = read_state_metadata(str(db))
    before = capture_state_snapshot(str(db), metadata=real, salt=salt)
    grown = capture_state_snapshot(
        str(db),
        metadata=StateFileMetadata(
            size_bytes=real.size_bytes + 4096, mtime_ns=real.mtime_ns + 1_000
        ),
        salt=salt,
    )
    comparison = compare_snapshots(before, grown)
    assert comparison.size_changed is True
    assert comparison.size_delta == 4096
    assert comparison.mtime_changed is True
    assert comparison.digest_equal is False
    assert comparison.unchanged is False


def test_user_version_change_is_detected(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    salt = new_run_salt()
    before = _snapshot(db, salt)
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
    finally:
        connection.close()
    after = _snapshot(db, salt)
    comparison = compare_snapshots(before, after)
    assert comparison.user_version_changed is True
    assert comparison.unchanged is False


# --------------------------------------------------------------------------
# 3. Fail-closed behaviour
# --------------------------------------------------------------------------


def test_missing_database_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(StateIntegrityError) as excinfo:
        read_state_metadata(str(tmp_path / "absent.db"))
    assert excinfo.value.code == "STATE_DB_UNREADABLE"


def test_unreadable_database_fails_closed(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    metadata = read_state_metadata(str(db))
    os.chmod(db, 0o000)
    try:
        if os.access(db, os.R_OK):  # pragma: no cover - root/CI permissive fs
            pytest.skip("filesystem does not enforce mode 0000")
        with pytest.raises(StateIntegrityError) as excinfo:
            capture_state_snapshot(str(db), metadata=metadata, salt=new_run_salt())
        assert excinfo.value.code == "STATE_DB_UNREADABLE"
    finally:
        os.chmod(db, 0o600)


def test_corrupt_database_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"this is not a sqlite database at all" * 8)
    with pytest.raises(StateIntegrityError) as excinfo:
        capture_state_snapshot(
            str(db), metadata=read_state_metadata(str(db)), salt=new_run_salt()
        )
    assert excinfo.value.code in {"STATE_DB_QUERY_FAILED", "STATE_DB_UNREADABLE"}


def test_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(StateIntegrityError) as excinfo:
        read_state_metadata(str(tmp_path))
    assert excinfo.value.code == "STATE_DB_NOT_REGULAR_FILE"


def test_relative_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(StateIntegrityError) as excinfo:
        capture_state_snapshot(
            "relative/state.db",
            metadata=StateFileMetadata(size_bytes=1, mtime_ns=1),
            salt=new_run_salt(),
        )
    assert excinfo.value.code == "STATE_DB_PATH_INVALID"


def test_short_salt_is_rejected(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    with pytest.raises(StateIntegrityError) as excinfo:
        capture_state_snapshot(
            str(db),
            metadata=read_state_metadata(str(db)),
            salt=b"0" * (MIN_SALT_BYTES - 1),
        )
    assert excinfo.value.code == "STATE_SALT_INVALID"


def test_negative_metadata_is_rejected() -> None:
    with pytest.raises(StateIntegrityError) as excinfo:
        StateFileMetadata(size_bytes=-1, mtime_ns=0)
    assert excinfo.value.code == "STATE_METADATA_INVALID"


def test_cross_run_salt_comparison_is_rejected(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    first = _snapshot(db, new_run_salt())
    second = _snapshot(db, new_run_salt())
    with pytest.raises(StateIntegrityError) as excinfo:
        compare_snapshots(first, second)
    assert excinfo.value.code == "STATE_SNAPSHOT_SALT_MISMATCH"


def test_salt_is_per_run_and_digest_is_salted(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    assert new_run_salt() != new_run_salt()
    a = _snapshot(db, new_run_salt())
    b = _snapshot(db, new_run_salt())
    assert a.as_canonical() == b.as_canonical()
    assert a.digest != b.digest


# --------------------------------------------------------------------------
# 4. Shadow / live disjointness
# --------------------------------------------------------------------------


def test_shadow_and_live_paths_must_be_disjoint(tmp_path: Path) -> None:
    live = tmp_path / "live" / "state.db"
    shadow = tmp_path / "shadow" / "state.db"
    live.parent.mkdir()
    shadow.parent.mkdir()
    assert_state_paths_disjoint(str(live), str(shadow))


def test_identical_paths_are_rejected(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with pytest.raises(StateIntegrityError) as excinfo:
        assert_state_paths_disjoint(str(db), str(db))
    assert excinfo.value.code == "STATE_PATHS_NOT_DISJOINT"


def test_same_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(StateIntegrityError) as excinfo:
        assert_state_paths_disjoint(
            str(tmp_path / "live.db"), str(tmp_path / "shadow.db")
        )
    assert excinfo.value.code == "STATE_PATHS_NOT_DISJOINT"


def test_nested_directory_is_rejected(tmp_path: Path) -> None:
    live = tmp_path / "outer" / "state.db"
    shadow = tmp_path / "outer" / "inner" / "state.db"
    with pytest.raises(StateIntegrityError) as excinfo:
        assert_state_paths_disjoint(str(live), str(shadow))
    assert excinfo.value.code == "STATE_PATHS_NOT_DISJOINT"


def test_sidecar_collision_is_rejected(tmp_path: Path) -> None:
    live = tmp_path / "a" / "state.db"
    shadow = tmp_path / "a" / "state.db-wal"
    with pytest.raises(StateIntegrityError) as excinfo:
        assert_state_paths_disjoint(str(live), str(shadow))
    assert excinfo.value.code == "STATE_PATHS_NOT_DISJOINT"


# --------------------------------------------------------------------------
# 5. No path / content / salt leak
# --------------------------------------------------------------------------


def test_snapshot_and_comparison_leak_nothing(tmp_path: Path) -> None:
    secret_dir = tmp_path / "very-secret-directory"
    secret_dir.mkdir()
    db = _make_state_db(secret_dir / "state.db")
    connection = sqlite3.connect(db)
    try:
        connection.execute("INSERT INTO messages (body) VALUES ('SENSITIVE-BODY')")
        connection.commit()
    finally:
        connection.close()

    salt = b"S" * MIN_SALT_BYTES
    snapshot = capture_state_snapshot(
        str(db), metadata=read_state_metadata(str(db)), salt=salt
    )
    rendered = json.dumps(snapshot.as_canonical()) + repr(snapshot)
    comparison = compare_snapshots(snapshot, snapshot)
    rendered += json.dumps(comparison.as_canonical())

    for forbidden in (
        "very-secret-directory",
        "state.db",
        "SENSITIVE-BODY",
        str(tmp_path),
        salt.decode(),
        salt.hex(),
    ):
        assert forbidden not in rendered


def test_error_repr_contains_no_path(tmp_path: Path) -> None:
    missing = tmp_path / "hidden-directory" / "state.db"
    with pytest.raises(StateIntegrityError) as excinfo:
        read_state_metadata(str(missing))
    text = repr(excinfo.value) + str(excinfo.value)
    assert "hidden-directory" not in text
    assert str(tmp_path) not in text


def test_comparison_payload_is_booleans_and_integers_only(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    salt = new_run_salt()
    comparison = compare_snapshots(_snapshot(db, salt), _snapshot(db, salt))
    payload = comparison.as_canonical()
    assert "digest" not in payload
    assert "salt" not in json.dumps(payload).lower()
    for key, value in payload.items():
        if key == "tables":
            continue
        assert isinstance(value, bool | int)


def test_snapshot_schema_is_versioned(tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    snapshot = _snapshot(db, new_run_salt())
    assert snapshot.schema == STATE_SNAPSHOT_SCHEMA
    assert snapshot.as_canonical()["schema"] == STATE_SNAPSHOT_SCHEMA


# --------------------------------------------------------------------------
# 6. Out-of-band orchestration: dry-run properties
# --------------------------------------------------------------------------


def _plan(tmp_path: Path, **overrides: object) -> TransientUnitPlan:
    kwargs: dict[str, object] = {
        "unit_name": "hermes-v2-oob-state-integrity",
        "probe_argv": ("/usr/bin/python3", "/opt/probe.py", "measure"),
        "working_directory": str(tmp_path),
        "result_path": str(tmp_path / "result.json"),
        "delay_seconds": 60,
        "timeout_seconds": 120,
    }
    kwargs.update(overrides)
    return build_transient_unit_plan(**kwargs)  # type: ignore[arg-type]


def test_plan_enforces_oneshot_no_restart_umask_and_timeout(tmp_path: Path) -> None:
    properties = _plan(tmp_path).property_map()
    assert properties["Type"] == "oneshot"
    assert properties["Restart"] == "no"
    assert properties["UMask"] == "0077"
    assert int(properties["RuntimeMaxSec"]) == 120
    assert properties["NoNewPrivileges"] == "yes"


def test_plan_schedules_a_delayed_transient_user_unit(tmp_path: Path) -> None:
    argv = _plan(tmp_path, delay_seconds=90).systemd_run_argv()
    assert argv[0] == "systemd-run"
    assert "--user" in argv
    assert "--collect" in argv
    assert "--on-active=90s" in argv


def test_plan_has_no_environment_assignments(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    assert plan.environment == ()
    rendered = plan.command_line()
    assert "Environment=" not in rendered
    assert "--setenv" not in rendered
    assert_plan_is_secret_free(plan)


def test_plan_rejects_secret_bearing_argv(tmp_path: Path) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(
            tmp_path,
            probe_argv=("/usr/bin/python3", "/opt/probe.py", "--token=abc"),
        )
    assert excinfo.value.code == "OOB_SECRET_BEARING_ARGUMENT"


@pytest.mark.parametrize(
    "argument",
    ["--api-key=x", "--password", "/opt/private_key.pem", "AUTHORIZATION"],
)
def test_plan_rejects_every_secret_token_class(tmp_path: Path, argument: str) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, probe_argv=("/usr/bin/python3", argument))
    assert excinfo.value.code == "OOB_SECRET_BEARING_ARGUMENT"


@pytest.mark.parametrize("delay", [0, -1, 100_000])
def test_plan_rejects_out_of_range_delay(tmp_path: Path, delay: int) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, delay_seconds=delay)
    assert excinfo.value.code == "OOB_DELAY_OUT_OF_RANGE"


@pytest.mark.parametrize("timeout", [0, -5, 10_000])
def test_plan_rejects_out_of_range_timeout(tmp_path: Path, timeout: int) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, timeout_seconds=timeout)
    assert excinfo.value.code == "OOB_TIMEOUT_OUT_OF_RANGE"


def test_plan_rejects_bad_unit_name(tmp_path: Path) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, unit_name="bad name;rm -rf /")
    assert excinfo.value.code == "OOB_UNIT_NAME_INVALID"


def test_plan_rejects_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, working_directory="relative/dir")
    assert excinfo.value.code == "OOB_PATH_NOT_ABSOLUTE"
    with pytest.raises(OutOfBandError) as excinfo:
        _plan(tmp_path, probe_argv=("python3", "measure"))
    assert excinfo.value.code == "OOB_PATH_NOT_ABSOLUTE"


def test_dry_run_executes_nothing(tmp_path: Path) -> None:
    def _runner(_: tuple[str, ...]) -> None:  # pragma: no cover - must not run
        raise AssertionError("dry-run must not execute anything")

    report = schedule_transient_unit(_plan(tmp_path), execute=False, runner=_runner)
    assert report["mode"] == "DRY_RUN"
    assert report["executed"] is False
    assert report == dry_run_report(_plan(tmp_path))


def test_execution_without_runner_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        schedule_transient_unit(_plan(tmp_path), execute=True, runner=None)
    assert excinfo.value.code == "OOB_EXECUTION_NOT_PERMITTED"


def test_dry_run_report_is_sanitized(tmp_path: Path) -> None:
    report = dry_run_report(_plan(tmp_path))
    text = json.dumps(report)
    assert "token" not in text.lower()
    assert report["environment_count"] == 0


# --------------------------------------------------------------------------
# 7. Atomic sanitized terminal marker
# --------------------------------------------------------------------------


def test_terminal_marker_is_atomic_and_private(tmp_path: Path) -> None:
    destination = tmp_path / "out" / "result.json"
    write_terminal_marker(
        str(destination), state=MARKER_COMPLETED, payload={"comparison": {"ok": True}}
    )
    mode = stat.S_IMODE(os.stat(destination).st_mode)
    assert mode == 0o600
    payload = read_terminal_marker(str(destination))
    assert payload is not None
    assert payload["state"] == MARKER_COMPLETED
    assert payload["schema"] == OUT_OF_BAND_SCHEMA
    # No temporary residue is left behind by the atomic replace.
    assert [item.name for item in destination.parent.iterdir()] == ["result.json"]


def test_terminal_marker_overwrite_is_atomic(tmp_path: Path) -> None:
    destination = tmp_path / "result.json"
    write_terminal_marker(str(destination), state=MARKER_COMPLETED, payload={"n": 1})
    write_terminal_marker(str(destination), state=MARKER_FAILED, payload={"reason": "X"})
    payload = read_terminal_marker(str(destination))
    assert payload is not None
    assert payload["state"] == MARKER_FAILED
    assert payload["reason"] == "X"
    assert not list(destination.parent.glob(".result.json.*"))


def test_invalid_marker_state_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        write_terminal_marker(
            str(tmp_path / "result.json"), state="PENDING", payload={}
        )
    assert excinfo.value.code == "OOB_MARKER_STATE_INVALID"


def test_marker_rejects_secret_bearing_keys(tmp_path: Path) -> None:
    with pytest.raises(OutOfBandError) as excinfo:
        write_terminal_marker(
            str(tmp_path / "result.json"),
            state=MARKER_COMPLETED,
            payload={"api_key": "value"},
        )
    assert excinfo.value.code == "OOB_SECRET_BEARING_ARGUMENT"


def test_partial_marker_is_not_accepted(tmp_path: Path) -> None:
    destination = tmp_path / "result.json"
    destination.write_text('{"schema": "hermes-v2-phase2', encoding="utf-8")
    assert read_terminal_marker(str(destination)) is None


def test_foreign_schema_marker_is_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "result.json"
    destination.write_text(
        json.dumps({"schema": "other/1", "state": MARKER_COMPLETED}), encoding="utf-8"
    )
    assert read_terminal_marker(str(destination)) is None


# --------------------------------------------------------------------------
# 8. Idempotent cleanup
# --------------------------------------------------------------------------


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    first = tmp_path / "result.json"
    second = tmp_path / "plan.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")

    removed = cleanup_run_artifacts(str(first), str(second))
    assert set(removed) == {"result.json", "plan.json"}
    assert cleanup_run_artifacts(str(first), str(second)) == ()
    assert cleanup_run_artifacts(str(tmp_path / "never-existed")) == ()


def test_cleanup_returns_basenames_only(tmp_path: Path) -> None:
    artifact = tmp_path / "secret-dir-name" / "result.json"
    artifact.parent.mkdir()
    artifact.write_text("{}", encoding="utf-8")
    removed = cleanup_run_artifacts(str(artifact))
    assert removed == ("result.json",)
    assert "secret-dir-name" not in "".join(removed)


# --------------------------------------------------------------------------
# 9. Static guarantees about the shipped scripts
# --------------------------------------------------------------------------


def _instructions(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_wrapper_script_is_dry_run_by_default_and_secret_free() -> None:
    text = _instructions(_WRAPPER.read_text(encoding="utf-8"))
    assert "umask 077" in text
    assert "EXECUTE_OUT_OF_BAND" in text
    assert "--property=Restart=no" in text
    assert "--property=UMask=0077" in text
    assert "--property=Type=oneshot" in text
    assert "RuntimeMaxSec" in text
    assert "--setenv" not in text
    assert "Environment=" not in text


def test_planner_script_never_executes_a_real_acceptance() -> None:
    text = _planner_source = _PLANNER.read_text(encoding="utf-8")
    assert "systemd-run" not in _instructions(_planner_source)
    assert "subprocess" not in _instructions(text)


def test_modules_are_not_imported_by_v1_surface() -> None:
    root = _MODULE_DIR / "src" / "hermes_mcp_bridge"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if "/v2/" in path.as_posix():
            continue
        source = path.read_text(encoding="utf-8")
        if "state_integrity" in source or "out_of_band" in source:
            offenders.append(path.name)
    assert offenders == []


def test_reason_codes_are_stable_and_uppercase() -> None:
    from hermes_mcp_bridge.v2.out_of_band import REASON_CODES as OOB_CODES
    from hermes_mcp_bridge.v2.state_integrity import REASON_CODES as STATE_CODES

    for code in set(OOB_CODES) | set(STATE_CODES):
        assert code == code.upper()
        assert " " not in code


def test_docs_state_that_zero_delta_requires_out_of_band() -> None:
    doc = _MODULE_DIR / "docs" / "v2" / "phase2-out-of-band-state-integrity.md"
    text = doc.read_text(encoding="utf-8")
    assert "out-of-band" in text.lower()
    assert "NOT ACCEPTED" in text
