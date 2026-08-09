"""Migration and startup contract tests for 0.6.1."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_mcp_bridge.approvals import ApprovalRegistry
from hermes_mcp_bridge.checkpoints import CheckpointRegistry
from hermes_mcp_bridge.locks import LockRegistry
from hermes_mcp_bridge.migrations import (
    _current_version,
    apply_migrations,
)
from hermes_mcp_bridge.plans import PlanStore
from hermes_mcp_bridge.quotas import QuotaRegistry
from hermes_mcp_bridge.registry import RunRegistry
from hermes_mcp_bridge.sagas import SagaRegistry


def _fresh_db(tmp_path: Path) -> str:
    return str(tmp_path / "state.sqlite3")


def test_migrations_are_idempotent_after_restart(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    first = apply_migrations(db_path)
    assert first == 10
    assert _current_version(sqlite3.connect(db_path, check_same_thread=False)) == 10
    second = apply_migrations(db_path)
    assert second == 10
    assert _current_version(sqlite3.connect(db_path, check_same_thread=False)) == 10


def test_empty_db_creates_canonical_schema(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    apply_migrations(db_path)
    RunRegistry(db_path).initialize()
    PlanStore(db_path).initialize()
    ApprovalRegistry(db_path).initialize()
    CheckpointRegistry(db_path).initialize()
    LockRegistry(db_path).initialize()
    SagaRegistry(db_path).initialize()
    QuotaRegistry(db_path).initialize()

    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        }
        expected = {
            "schema_migrations",
            "run_mappings",
            "plans",
            "plan_approvals",
            "checkpoints",
            "continuations",
            "sagas",
            "resource_locks",
            "quota_profiles",
            "approvals",
        }
        assert expected.issubset(tables)
    finally:
        conn.close()


def test_checkpoint_schema_contains_plan_id(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    apply_migrations(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(checkpoints)").fetchall()}
        assert "plan_id" in cols
    finally:
        conn.close()


def test_continuations_schema_is_canonical(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    apply_migrations(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(continuations)").fetchall()}
        for col in {"continuation_id", "execution_id", "checkpoint_id", "mode"}:
            assert col in cols
    finally:
        conn.close()


def test_approvals_created_by_migration_v9(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    apply_migrations(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        version = _current_version(conn)
        assert version == 10
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='approvals'"
        )
        assert cursor.fetchone() is not None
    finally:
        conn.close()


def test_startup_preserves_non_sensitive_row_idempotently(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    apply_migrations(db_path)
    registry = RunRegistry(db_path)
    registry.initialize()
    registry.record(
        client_request_id="idempotent-1",
        fingerprint="a" * 64,
        execution_id="run-1",
        last_status="queued",
    )

    first = apply_migrations(db_path)
    assert first == 10

    mapping = registry.get("idempotent-1")
    assert mapping is not None
    assert mapping["execution_id"] == "run-1"

    second = apply_migrations(db_path)
    assert second == 10

    mapping = registry.get("idempotent-1")
    assert mapping is not None
    assert mapping["execution_id"] == "run-1"


def test_legacy_run_mappings_only_db_upgrades_to_v10(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    legacy = sqlite3.connect(db_path, check_same_thread=False)
    try:
        legacy.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_mappings (
                client_request_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                session_id TEXT,
                last_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO run_mappings
                (client_request_id, fingerprint, execution_id, last_status, created_at, updated_at)
            VALUES (
                'legacy-1', 'fp', 'run-legacy', 'queued',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );
            """
        )
    finally:
        legacy.close()

    apply_migrations(db_path)
    assert _current_version(sqlite3.connect(db_path, check_same_thread=False)) == 10

    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        row = conn.execute(
            "SELECT client_request_id FROM run_mappings WHERE client_request_id = 'legacy-1'"
        ).fetchone()
        assert row is not None
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='approvals'"
        )
        assert cursor.fetchone() is not None
    finally:
        conn.close()


def test_missing_parent_dir_creates_nested_db_path(tmp_path: Path) -> None:
    db_path = str(tmp_path / "nested" / "missing" / "state.sqlite3")
    apply_migrations(db_path)
    assert Path(db_path).is_file()
    assert _current_version(sqlite3.connect(db_path, check_same_thread=False)) == 10


def test_empty_db_has_run_mappings_updated_at_index(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    apply_migrations(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name='idx_run_mappings_updated_at'"
        )
        assert cursor.fetchone() is not None
    finally:
        conn.close()


def test_legacy_run_mappings_upgrade_creates_index_preserves_row(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    legacy = sqlite3.connect(db_path, check_same_thread=False)
    try:
        legacy.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_mappings (
                client_request_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                session_id TEXT,
                last_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO run_mappings
                (client_request_id, fingerprint, execution_id, last_status, created_at, updated_at)
            VALUES (
                'legacy-1', 'fp', 'run-legacy', 'queued',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );
            """
        )
    finally:
        legacy.close()

    apply_migrations(db_path)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        version = _current_version(conn)
        assert version == 10
        row = conn.execute(
            "SELECT client_request_id FROM run_mappings WHERE client_request_id = 'legacy-1'"
        ).fetchone()
        assert row is not None
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name='idx_run_mappings_updated_at'"
        )
        assert cursor.fetchone() is not None
    finally:
        conn.close()


def test_second_startup_is_idempotent_and_index_remains_unique(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    apply_migrations(db_path)
    apply_migrations(db_path)
    apply_migrations(db_path)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        version = _current_version(conn)
        assert version == 10
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='run_mappings'"
            " AND name='idx_run_mappings_updated_at'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


def test_non_writable_parent_propagates_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = str(tmp_path / "locked" / "state.sqlite3")
    parent = Path(db_path).parent
    parent.mkdir(parents=True, exist_ok=True)
    real_mkdir = Path.mkdir

    def _raising_mkdir(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if self == parent:
            raise PermissionError("parent not writable")
        return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", _raising_mkdir, raising=False)

    with pytest.raises(PermissionError):
        apply_migrations(db_path)
