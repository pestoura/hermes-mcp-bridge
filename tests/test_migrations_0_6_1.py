"""Migration and startup contract tests for 0.6.1."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_mcp_bridge.approvals import ApprovalRegistry
from hermes_mcp_bridge.checkpoints import CheckpointRegistry
from hermes_mcp_bridge.locks import LockRegistry
from hermes_mcp_bridge.migrations import _current_version, apply_migrations
from hermes_mcp_bridge.plans import PlanStore
from hermes_mcp_bridge.quotas import QuotaRegistry
from hermes_mcp_bridge.registry import RunRegistry
from hermes_mcp_bridge.sagas import SagaRegistry


def _fresh_db(tmp_path: Path) -> str:
    return str(tmp_path / "state.sqlite3")


def test_migrations_are_idempotent_after_restart(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    first = apply_migrations(db_path)
    assert first == 9
    assert _current_version(sqlite3.connect(db_path, check_same_thread=False)) == 9
    second = apply_migrations(db_path)
    assert second == 9
    assert _current_version(sqlite3.connect(db_path, check_same_thread=False)) == 9


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
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(checkpoints)").fetchall()
        }
        assert "plan_id" in cols
    finally:
        conn.close()


def test_continuations_schema_is_canonical(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    apply_migrations(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(continuations)").fetchall()
        }
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
        assert version == 9
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
    assert first == 9

    mapping = registry.get("idempotent-1")
    assert mapping is not None
    assert mapping["execution_id"] == "run-1"

    second = apply_migrations(db_path)
    assert second == 9

    mapping = registry.get("idempotent-1")
    assert mapping is not None
    assert mapping["execution_id"] == "run-1"


def test_legacy_run_mappings_only_db_upgrades_to_v9(tmp_path: Path) -> None:
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
    assert _current_version(sqlite3.connect(db_path, check_same_thread=False)) == 9

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
