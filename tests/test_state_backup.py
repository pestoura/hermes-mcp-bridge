"""Regression tests for online SQLite backup/restore hardening.

These tests never touch production state. They operate on isolated
temporary databases created in the pytest temp directory.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from hermes_mcp_bridge import state_backup
from hermes_mcp_bridge.state_backup import (
    _integrity_check,
    backup_state_db,
    restore_state_db,
    verify_backup,
)


@pytest.fixture()
def settings_stub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    class _S:
        bridge_state_db_path = str(tmp_path / "state.sqlite3")
        bridge_version = "0.7.0-test"

    monkeypatch.setattr(state_backup, "get_settings", lambda: _S())
    monkeypatch.setattr(state_backup, "_SETTINGS", _S())
    return _S()


def _seed(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO runs (name) VALUES ('alpha'), ('beta')")
    conn.commit()
    conn.close()


def _enable_wal(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()


def test_backup_online_consistent_with_wal(
    settings_stub: object, tmp_path: Path
) -> None:
    settings = settings_stub  # type: ignore[assignment]
    db = settings.bridge_state_db_path  # type: ignore[attr-defined]
    _seed(db)
    _enable_wal(db)
    # concurrent writer while backup runs
    writer = sqlite3.connect(db, timeout=5)
    writer.execute("INSERT INTO runs (name) VALUES ('gamma')")
    writer.commit()
    backup_path = str(tmp_path / "nested" / "dir" / "state.sqlite3.backup")
    result = backup_state_db(db, backup_path)
    writer.close()
    assert result["status"] == "ok"
    assert result["backup"] == backup_path
    assert os.path.exists(backup_path)
    # mode 0600
    mode = stat.S_IMODE(os.stat(backup_path).st_mode)
    assert mode == 0o600
    # integrity of backup
    bconn = sqlite3.connect(backup_path)
    assert _integrity_check(bconn)
    rows = bconn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    bconn.close()
    assert rows >= 3


def test_backup_dry_run_does_not_write(settings_stub: object, tmp_path: Path) -> None:
    settings = settings_stub  # type: ignore[assignment]
    db = settings.bridge_state_db_path  # type: ignore[attr-defined]
    _seed(db)
    backup_path = str(tmp_path / "state.sqlite3.backup")
    result = backup_state_db(db, backup_path, dry_run=True)
    assert result["status"] == "dry_run"
    assert not os.path.exists(backup_path)
    assert "metadata" in result
    assert result["metadata"]["integrity_ok"] in (True, False)


def test_backup_metadata_sanitized_no_secrets(
    settings_stub: object, tmp_path: Path
) -> None:
    settings = settings_stub  # type: ignore[assignment]
    db = settings.bridge_state_db_path  # type: ignore[attr-defined]
    _seed(db)
    backup_path = str(tmp_path / "state.sqlite3.backup")
    result = backup_state_db(db, backup_path)
    meta = result["metadata"]
    assert "rows" not in meta
    assert "data" not in meta
    for key in ("source_sha256_prefix", "backup_sha256_prefix"):
        assert isinstance(meta[key], str)
        assert len(meta[key]) <= 16
    assert meta["bridge_version"] == "0.7.0-test"
    assert meta["mode"] == 0o600


def test_restore_round_trip(settings_stub: object, tmp_path: Path) -> None:
    settings = settings_stub  # type: ignore[assignment]
    db = settings.bridge_state_db_path  # type: ignore[attr-defined]
    _seed(db)
    backup_path = str(tmp_path / "state.sqlite3.backup")
    backup_state_db(db, backup_path)

    # mutate source then restore
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM runs")
    conn.commit()
    conn.close()

    # target not opened by a writer -> restore proceeds
    res = restore_state_db(backup_path, db, force=True)
    assert res["status"] == "ok"
    assert Path(res["previous_target_backup"]).exists()
    rconn = sqlite3.connect(db)
    count = rconn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    rconn.close()
    assert count == 2


def test_restore_refuses_active_writer(
    settings_stub: object, tmp_path: Path
) -> None:
    settings = settings_stub  # type: ignore[assignment]
    db = settings.bridge_state_db_path  # type: ignore[attr-defined]
    _seed(db)
    backup_path = str(tmp_path / "state.sqlite3.backup")
    backup_state_db(db, backup_path)
    # hold a writer connection (simulates active bridge)
    writer = sqlite3.connect(db, timeout=1)
    writer.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError):
        restore_state_db(backup_path, db, force=False)
    writer.rollback()
    writer.close()


def test_restore_preserves_previous_target(
    settings_stub: object, tmp_path: Path
) -> None:
    settings = settings_stub  # type: ignore[assignment]
    db = settings.bridge_state_db_path  # type: ignore[attr-defined]
    _seed(db)
    backup_path = str(tmp_path / "state.sqlite3.backup")
    backup_state_db(db, backup_path)
    res = restore_state_db(backup_path, db, force=True)
    prev = Path(res["previous_target_backup"])
    assert prev.exists()
    mode = stat.S_IMODE(os.stat(prev).st_mode)
    assert mode == 0o600


def test_restore_corrupt_backup_fail_closed(
    settings_stub: object, tmp_path: Path
) -> None:
    settings = settings_stub  # type: ignore[assignment]
    db = settings.bridge_state_db_path  # type: ignore[attr-defined]
    _seed(db)
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database at all")
    with pytest.raises(RuntimeError):
        restore_state_db(str(corrupt), db, force=True)


def test_verify_backup_reports_compatibility(
    settings_stub: object, tmp_path: Path
) -> None:
    settings = settings_stub  # type: ignore[assignment]
    db = settings.bridge_state_db_path  # type: ignore[attr-defined]
    _seed(db)
    backup_path = str(tmp_path / "state.sqlite3.backup")
    backup_state_db(db, backup_path)
    # simulate higher-version backup vs source
    bconn = sqlite3.connect(backup_path)
    bconn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER)"
    )
    bconn.execute("INSERT INTO schema_migrations VALUES (99)")
    bconn.commit()
    bconn.close()
    report = verify_backup(backup_path, db)
    assert report["backup_integrity"] is True
    assert report["compatible"] is False  # backup newer than source


def test_backup_source_missing_fail_closed(
    settings_stub: object, tmp_path: Path
) -> None:
    missing = str(tmp_path / "does-not-exist.sqlite3")
    with pytest.raises(FileNotFoundError):
        backup_state_db(missing, str(tmp_path / "out.backup"))


def test_backup_mode_is_0600_and_nested_dirs(
    settings_stub: object, tmp_path: Path
) -> None:
    settings = settings_stub  # type: ignore[assignment]
    db = settings.bridge_state_db_path  # type: ignore[attr-defined]
    _seed(db)
    backup_path = str(tmp_path / "a" / "b" / "c" / "state.sqlite3.backup")
    backup_state_db(db, backup_path)
    assert os.path.exists(backup_path)
    assert os.path.isdir(tmp_path / "a" / "b" / "c")
    mode = stat.S_IMODE(os.stat(backup_path).st_mode)
    assert mode == 0o600
