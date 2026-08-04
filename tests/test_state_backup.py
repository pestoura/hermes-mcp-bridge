from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import hermes_mcp_bridge.state_backup as sb
from hermes_mcp_bridge.state_backup import (
    BackupMetadata,
    WriterState,
    backup_state_db,
    restore_state_db,
    verify_backup,
)


def _make_db(path, rows=3):
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.isolation_level = None
    conn.execute("CREATE TABLE schema_migrations(version INTEGER)")
    conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")
    conn.execute("CREATE TABLE t(id INTEGER)")
    for i in range(rows):
        conn.execute(f"INSERT INTO t(id) VALUES ({i})")
    conn.commit()
    conn.close()


def test_basic_roundtrip(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    res = backup_state_db(str(db))
    assert res["status"] == "ok"
    assert os.path.exists(res["backup"])
    meta = res["metadata"]
    assert meta["online_backup"] is True
    assert meta["integrity_ok"] is True

    target = tmp_path / "restore.sqlite3"
    r = restore_state_db(res["backup"], str(target))
    assert r["status"] == "ok"
    assert r["restored_from"] == res["backup"]
    # verify round-trip count
    conn = sqlite3.connect(str(target))
    n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert n == 3


def test_backup_metadata_sanitized_no_secrets(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    res = backup_state_db(str(db))
    text = str(res["metadata"])
    assert "secret" not in text.lower() or "sha256" in text.lower()
    # no full 64-hex sha (only prefix)
    assert len(res["metadata"]["source_sha256_prefix"]) <= 32
    assert len(res["metadata"]["backup_sha256_prefix"]) <= 32


def test_backup_mode_is_0600_and_nested_dirs(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    nested = tmp_path / "a" / "b" / "c" / "backup.sqlite3"
    res = backup_state_db(str(db), str(nested))
    mode = os.stat(res["backup"]).st_mode & 0o777
    assert mode == 0o600
    assert nested.parent.exists()


def test_backup_unique_names_no_overwrite(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    a = backup_state_db(str(db))
    b = backup_state_db(str(db))
    assert a["backup"] != b["backup"]
    with pytest.raises(FileExistsError):
        backup_state_db(str(db), a["backup"])


def test_backup_overwrite_allowed(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    first = backup_state_db(str(db))
    second = backup_state_db(str(db), first["backup"], overwrite=True)
    assert second["status"] == "ok"


def test_backup_retention_only_own_names(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    [backup_state_db(str(db))["backup"] for _ in range(4)]
    # drop an external-looking file that must NOT be deleted
    external = tmp_path / "state.sqlite3.evil-backup"
    external.write_text("nope")
    res = backup_state_db(str(db), retention_count=2)
    removed = res["removed_retained"]
    assert external.exists()
    # 4 prior backups + this one = 5 own backups; keep 2, remove 3
    assert len(removed) == 3
    for r in removed:
        assert sb.BACKUP_NAME_RE.match(os.path.basename(r))


def test_backup_online_consistent_with_wal(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    conn = sqlite3.connect(str(db))
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO t(id) VALUES (999)")
    res = backup_state_db(str(db))
    assert res["status"] == "ok"
    conn.execute("COMMIT")
    conn.close()
    # backup should be a valid, integrity-ok DB
    conn2 = sqlite3.connect(res["backup"])
    n = conn2.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn2.close()
    # backup reads last committed state (writer insert not committed)
    assert n >= 3


def test_backup_dry_run_no_artifacts(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    before = set(p.name for p in tmp_path.iterdir())
    res = backup_state_db(str(db), dry_run=True)
    assert res["status"] == "dry_run"
    after = set(p.name for p in tmp_path.iterdir())
    assert before == after


def test_backup_source_missing_fail_closed(tmp_path):
    missing = tmp_path / "nope.sqlite3"
    with pytest.raises(FileNotFoundError):
        backup_state_db(str(missing))


def test_restore_force_cannot_bypass_path_safety(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    res = backup_state_db(str(db))
    link = tmp_path / "evil_link.sqlite3"
    os.symlink(res["backup"], str(link))
    with pytest.raises(ValueError):
        restore_state_db(str(link), tmp_path / "out.sqlite3", force=True)


def test_restore_symlink_target_rejected(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    res = backup_state_db(str(db))
    out_link = tmp_path / "out_link.sqlite3"
    os.symlink(tmp_path / "x.sqlite3", str(out_link))
    with pytest.raises(ValueError):
        restore_state_db(res["backup"], str(out_link), force=True)


def test_restore_refuses_active_writer(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    res = backup_state_db(str(db))
    writer = sqlite3.connect(str(db))
    writer.isolation_level = None
    writer.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError):
        restore_state_db(res["backup"], str(db))
    writer.execute("COMMIT")
    writer.close()


def test_restore_unknown_writer_blocks(tmp_path, monkeypatch):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    res = backup_state_db(str(db))

    def _boom(path):
        return WriterState.UNKNOWN

    monkeypatch.setattr(sb, "_detect_writer_state", _boom)
    with pytest.raises(RuntimeError):
        restore_state_db(res["backup"], str(db), force=False)


def test_restore_internal_rollback_on_failure(tmp_path, monkeypatch):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    res = backup_state_db(str(db))
    target = tmp_path / "target.sqlite3"

    real_replace = os.replace

    def _bad_replace(src, dst):
        real_replace(src, dst)
        raise RuntimeError("simulated write failure after first replace")

    monkeypatch.setattr(os, "replace", _bad_replace)
    with pytest.raises(RuntimeError):
        restore_state_db(res["backup"], str(target))
    # target should be restored from bundle
    assert target.exists()
    conn = sqlite3.connect(str(target))
    n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert n == 3


def test_restore_previous_backup_bundle_created(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    res = backup_state_db(str(db))
    target = tmp_path / "target.sqlite3"
    # pre-create target so a rollback bundle is produced; use force because the
    # stale placeholder is not a valid DB (writer state would be unknown)
    target.write_text("stale")
    r = restore_state_db(res["backup"], str(target), force=True)
    assert r["previous_target_backup"]
    assert Path(r["previous_target_backup"]).exists()


def test_verify_backup_reports_compatibility(tmp_path):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    res = backup_state_db(str(db))
    v = verify_backup(res["backup"], str(db))
    assert v["backup_integrity"] is True
    assert v["compatible"] is True
    assert v["online_backup"] is True


def test_allowed_root_escape_rejected(tmp_path, monkeypatch):
    db = tmp_path / "state.sqlite3"
    _make_db(db)
    outside = tmp_path.parent / "outside.sqlite3"
    _make_db(outside)
    monkeypatch.setenv(sb.ALLOWED_ROOT_ENV, str(tmp_path))
    with pytest.raises(ValueError):
        backup_state_db(str(outside))


def test_backup_metadata_dataclass(tmp_path):
    m = BackupMetadata(
        timestamp_utc="2026-08-04T00:00:00+00:00",
        schema_migrations_count=1,
        schema_migrations_version=1,
        source_size_bytes=10,
        backup_size_bytes=10,
        source_sha256_prefix="abc",
        backup_sha256_prefix="def",
        owner_uid=1000,
        mode=0o600,
        bridge_version="0.7.0",
    )
    d = m.as_dict()
    assert d["bridge_version"] == "0.7.0"
    assert d["online_backup"] is True
