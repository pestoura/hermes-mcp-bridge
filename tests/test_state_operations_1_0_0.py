"""Directed tests for 1.0.0 SQLite diagnostics and maintenance gates."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_mcp_bridge.config import get_settings
from hermes_mcp_bridge.migrations import apply_migrations
from hermes_mcp_bridge.state_backup import backup_state_db
from hermes_mcp_bridge.state_operations import (
    StateThresholds,
    checkpoint_state_db,
    diagnose_state_db,
    safe_diagnostic_summary,
    verify_restore_in_isolation,
)

NOW = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configured_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db = tmp_path / "state.sqlite3"
    monkeypatch.setenv("HERMES_API_KEY", "test-key")
    monkeypatch.setenv("BRIDGE_SECURITY_MODE", "test")
    monkeypatch.setenv("BRIDGE_STATE_DB_PATH", str(db))
    monkeypatch.setenv("HERMES_BRIDGE_BACKUP_ROOT", str(tmp_path))
    get_settings.cache_clear()
    apply_migrations(str(db))
    return db


def _seed_operational_state(db: Path) -> None:
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO run_mappings VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "sensitive-client-request-id",
                "sensitive-fingerprint",
                "sensitive-execution-id",
                "sensitive-session-id",
                "running",
                _iso(NOW - timedelta(hours=3)),
                _iso(NOW - timedelta(hours=2)),
            ),
        )
        connection.execute(
            "INSERT INTO run_mappings VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "completed-request-id",
                "completed-fingerprint",
                "completed-execution-id",
                None,
                "completed",
                _iso(NOW - timedelta(hours=3)),
                _iso(NOW - timedelta(hours=2)),
            ),
        )
        connection.execute(
            """
            INSERT INTO resource_locks(
                lock_key, lock_type, owner, execution_id, ttl_seconds,
                acquired_at, expires_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sensitive-lock-key",
                "WRITE_EXCLUSIVE",
                "sensitive-owner",
                "sensitive-execution-id",
                60,
                _iso(NOW - timedelta(hours=2)),
                _iso(NOW - timedelta(hours=1)),
                "active",
            ),
        )
        connection.execute(
            """
            INSERT INTO approvals(
                approval_id, action, decision, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "sensitive-approval-id",
                "sensitive-action",
                "requested",
                _iso(NOW - timedelta(minutes=1)),
                _iso(NOW - timedelta(hours=1)),
            ),
        )
        connection.execute(
            """
            INSERT INTO sagas(
                saga_id, execution_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "sensitive-saga-id",
                "sensitive-execution-id",
                "running",
                _iso(NOW - timedelta(hours=3)),
                _iso(NOW - timedelta(hours=2)),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_diagnostic_is_aggregate_read_only_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = _configured_db(monkeypatch, tmp_path)
    _seed_operational_state(db)
    before = _sha256(db)

    payload = diagnose_state_db(
        str(db),
        now=NOW,
        thresholds=StateThresholds(
            stale_after_seconds=3600,
            wal_warning_bytes=0,
            db_warning_bytes=10**12,
            free_warning_bytes=0,
        ),
    )
    after = _sha256(db)
    serialized = str(payload)

    assert before == after
    assert payload["read_only"] is True
    assert payload["quick_check_ok"] is True
    assert payload["schema"]["migration_version"] == 10
    assert payload["runs"]["non_terminal"] == 1
    assert payload["runs"]["stale"] == 1
    assert payload["locks"]["active_expired"] == 1
    assert payload["approvals"]["pending_expired"] == 1
    assert payload["sagas"]["stale"] == 1
    assert payload["checkpoint"]["recommended"] is True
    assert payload["warnings"] == [
        "expired_approvals",
        "stale_locks",
        "stale_runs",
        "stale_sagas",
        "wal_size",
    ]
    for sensitive in (
        "sensitive-client-request-id",
        "sensitive-fingerprint",
        "sensitive-execution-id",
        "sensitive-session-id",
        "sensitive-lock-key",
        "sensitive-owner",
        "sensitive-approval-id",
        "sensitive-action",
        "sensitive-saga-id",
    ):
        assert sensitive not in serialized
    assert str(db) not in serialized


def test_safe_summary_has_fixed_public_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = _configured_db(monkeypatch, tmp_path)
    payload = diagnose_state_db(str(db), now=NOW)
    payload["unexpected"] = "must-not-leak"

    summary = safe_diagnostic_summary(payload)

    assert "unexpected" not in summary
    assert set(summary) == {
        "status",
        "read_only",
        "quick_check_ok",
        "schema",
        "sqlite",
        "storage",
        "tables",
        "runs",
        "locks",
        "approvals",
        "sagas",
        "checkpoint",
        "warnings",
    }


def test_checkpoint_defaults_to_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = _configured_db(monkeypatch, tmp_path)

    result = checkpoint_state_db(str(db))

    assert result["status"] == "dry_run"
    assert result["execute"] is False
    assert result["mode"] == "TRUNCATE"


def test_checkpoint_rejects_unknown_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = _configured_db(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="PASSIVE/FULL/RESTART/TRUNCATE"):
        checkpoint_state_db(str(db), mode="unsafe")


def test_checkpoint_execution_truncates_wal_when_writer_is_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = _configured_db(monkeypatch, tmp_path)
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE IF NOT EXISTS checkpoint_probe(value BLOB)")
        writer.executemany(
            "INSERT INTO checkpoint_probe(value) VALUES (?)",
            [(os.urandom(2048),) for _ in range(128)],
        )
        writer.commit()
        wal = Path(f"{db}-wal")
        assert wal.exists()
        before = wal.stat().st_size
        assert before > 0

        result = checkpoint_state_db(str(db), mode="TRUNCATE", execute=True)

        assert result["status"] == "ok"
        assert result["busy"] == 0
        assert result["wal_bytes_before"] == before
        assert result["wal_bytes_after"] == 0
    finally:
        writer.close()


def test_checkpoint_fails_closed_on_active_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = _configured_db(monkeypatch, tmp_path)
    writer = sqlite3.connect(db)
    writer.isolation_level = None
    writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError, match="checkpoint blocked"):
            checkpoint_state_db(str(db), execute=True)
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_isolated_restore_exercises_real_restore_and_cleans_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = _configured_db(monkeypatch, tmp_path)
    _seed_operational_state(db)
    backup = tmp_path / "candidate.backup.sqlite3"
    result = backup_state_db(str(db), str(backup))
    assert result["status"] == "ok"

    proof = verify_restore_in_isolation(str(backup))

    assert proof["status"] == "pass"
    assert proof["isolated_restore"] is True
    assert proof["cleanup_ok"] is True
    assert proof["integrity_ok"] is True
    assert proof["migration_version"] == 10
    assert proof["migration_count"] == 10
    assert proof["table_count"] >= 10
    assert proof["restored_bytes"] > 0
    assert not list(tmp_path.glob(".restore-proof-*"))


def test_isolated_restore_rejects_corrupt_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configured_db(monkeypatch, tmp_path)
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")

    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        verify_restore_in_isolation(str(corrupt))


def test_threshold_validation_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="stale_after_seconds"):
        StateThresholds(stale_after_seconds=0)
    with pytest.raises(ValueError, match="wal_warning_bytes"):
        StateThresholds(wal_warning_bytes=-1)
