import os
import sqlite3

import pytest

from hermes_mcp_bridge import state_backup as sb


@pytest.fixture
def settings(monkeypatch, tmp_path):
    db = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE schema_migrations(version INTEGER)")
    conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")
    conn.commit()
    conn.close()
    cfg = {"bridge_state_db_path": str(db), "bridge_version": "0.7.0"}

    def _cfg():
        return type("S", (), cfg)()

    monkeypatch.setattr("hermes_mcp_bridge.state_backup.get_settings", _cfg)
    return type("S", (), cfg)()


def test_backup_creates_unique_file(settings, tmp_path):
    r1 = sb.backup_state_db()
    r2 = sb.backup_state_db()
    assert r1["status"] == "ok"
    assert r2["status"] == "ok"
    assert r1["backup"] != r2["backup"]
    assert os.path.exists(r1["backup"])
    assert os.path.exists(r2["backup"])


def test_backup_metadata_no_secrets(settings, tmp_path):
    r = sb.backup_state_db()
    meta = r["metadata"]
    blob = str(meta)
    assert "secret" not in blob.lower()
    assert meta["schema_migrations_count"] == 1
    assert meta["schema_migrations_version"] == 1
    assert meta["online_backup"] is True
    assert "rows" not in meta


def test_backup_mode_0600_and_nested_dirs(settings, tmp_path):
    r = sb.backup_state_db(backup_path=str(tmp_path / "a" / "b" / "c" / "state.backup"))
    assert r["status"] == "ok"
    mode = oct(os.stat(r["backup"]).st_mode & 0o777)
    assert mode == "0o600"
    assert (tmp_path / "a" / "b" / "c").exists()


def test_backup_dry_run_creates_no_files(settings, tmp_path):
    before = set(p.name for p in tmp_path.iterdir())
    r = sb.backup_state_db(dry_run=True)
    after = set(p.name for p in tmp_path.iterdir())
    assert r["status"] == "dry_run"
    assert before == after
    assert "temporary_backup" not in r


def test_backup_existing_target_fail_closed(settings, tmp_path):
    target = tmp_path / "explicit.backup"
    target.write_text("old")
    with pytest.raises(FileExistsError):
        sb.backup_state_db(backup_path=str(target))
    r = sb.backup_state_db(backup_path=str(target), overwrite=True)
    assert r["status"] == "ok"


def test_backup_online_consistent_with_wal(settings, tmp_path):
    db = settings.bridge_state_db_path
    writer = sqlite3.connect(db)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("CREATE TABLE IF NOT EXISTS t(x)")
    r = sb.backup_state_db()
    writer.commit()
    writer.close()
    assert r["status"] == "ok"
    bk = sqlite3.connect(r["backup"])
    assert bk.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    bk.close()


def test_backup_source_missing_fail_closed(settings, tmp_path, monkeypatch):
    cfg = {"bridge_state_db_path": str(tmp_path / "nope.sqlite3"), "bridge_version": "0.7.0"}

    def _cfg():
        return type("S", (), cfg)()

    monkeypatch.setattr("hermes_mcp_bridge.state_backup.get_settings", _cfg)
    with pytest.raises(FileNotFoundError):
        sb.backup_state_db()


def test_backup_source_corrupt_fail_closed(settings, tmp_path, monkeypatch):
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a sqlite database at all")
    with pytest.raises(RuntimeError):
        sb.backup_state_db(source_path=str(bad))


def test_restore_round_trip(settings, tmp_path):
    r = sb.backup_state_db()
    conn = sqlite3.connect(settings.bridge_state_db_path)
    conn.execute("CREATE TABLE extra(y)")
    conn.commit()
    conn.close()
    rr = sb.restore_state_db(r["backup"])
    assert rr["status"] == "ok"
    conn = sqlite3.connect(settings.bridge_state_db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "extra" not in tables
    assert "schema_migrations" in tables


def test_restore_prevents_stale_sidecars(settings, tmp_path):
    r = sb.backup_state_db()
    stray_wal = settings.bridge_state_db_path + "-wal"
    stray_shm = settings.bridge_state_db_path + "-shm"
    open(stray_wal, "w").close()
    open(stray_shm, "w").close()
    sb.restore_state_db(r["backup"])
    assert not os.path.exists(stray_wal)
    assert not os.path.exists(stray_shm)


def test_restore_internal_rollback_on_failure(settings, tmp_path, monkeypatch):
    r = sb.backup_state_db()
    conn = sqlite3.connect(settings.bridge_state_db_path)
    conn.execute("CREATE TABLE marker(z)")
    conn.commit()
    conn.close()
    orig_hash = sb._sha256_file(settings.bridge_state_db_path)

    def boom(*a, **k):
        raise RuntimeError("injected failure")

    monkeypatch.setattr("hermes_mcp_bridge.state_backup._integrity_check", boom)
    with pytest.raises(RuntimeError):
        sb.restore_state_db(r["backup"])
    assert os.path.exists(settings.bridge_state_db_path)
    assert sb._sha256_file(settings.bridge_state_db_path) == orig_hash


def test_restore_refuses_active_writer(settings, tmp_path):
    r = sb.backup_state_db()
    writer = sqlite3.connect(settings.bridge_state_db_path)
    writer.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError):
        sb.restore_state_db(r["backup"])
    writer.commit()
    writer.close()


def test_restore_requires_force_for_unknown_writer(settings, tmp_path, monkeypatch):
    r = sb.backup_state_db()
    monkeypatch.setattr(
        "hermes_mcp_bridge.state_backup._detect_writer_state",
        lambda p: sb.WriterState.UNKNOWN,
    )
    with pytest.raises(RuntimeError):
        sb.restore_state_db(r["backup"])
    rr = sb.restore_state_db(r["backup"], force=True)
    assert rr["status"] == "ok"


def test_restore_explicit_target(settings, tmp_path):
    r = sb.backup_state_db()
    dest = tmp_path / "other.sqlite3"
    rr = sb.restore_state_db(r["backup"], target_path=str(dest))
    assert rr["target"] == str(dest)
    assert os.path.exists(str(dest))


def test_verify_backup_compatibility(settings, tmp_path):
    r = sb.backup_state_db()
    v = sb.verify_backup(r["backup"])
    assert v["backup_integrity"] is True
    assert v["compatible"] is True
    assert v["online_backup"] is True
