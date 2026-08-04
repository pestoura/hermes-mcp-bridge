"""Schema migrations for the bridge state database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import get_settings


@dataclass(frozen=True)
class Migration:
    version: int
    label: str
    sql: str


_MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        label="baseline_run_mappings",
        sql="""
        CREATE TABLE IF NOT EXISTS run_mappings (
            client_request_id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            session_id TEXT,
            last_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    Migration(
        version=2,
        label="plans",
        sql="""
        CREATE TABLE IF NOT EXISTS plans (
            plan_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            version TEXT NOT NULL DEFAULT '1',
            status TEXT NOT NULL DEFAULT 'draft',
            steps_json TEXT NOT NULL DEFAULT '[]',
            dependencies_json TEXT NOT NULL DEFAULT '[]',
            risks_json TEXT NOT NULL DEFAULT '[]',
            approval_points_json TEXT NOT NULL DEFAULT '[]',
            parallel_groups_json TEXT NOT NULL DEFAULT '[]',
            critical_path_json TEXT NOT NULL DEFAULT '[]',
            locks_json TEXT NOT NULL DEFAULT '[]',
            budgets_json TEXT NOT NULL DEFAULT '{}',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT,
            updated_at TEXT,
            plan_hash TEXT,
            policy_json TEXT NOT NULL DEFAULT '{}',
            trace_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_plans_status ON plans (status);
        """,
    ),
    Migration(
        version=3,
        label="plan_approvals",
        sql="""
        CREATE TABLE IF NOT EXISTS plan_approvals (
            approval_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            approver TEXT,
            expires_at TEXT,
            consumed_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (plan_id) REFERENCES plans (plan_id)
        );
        CREATE INDEX IF NOT EXISTS idx_plan_approvals_plan ON plan_approvals (plan_id);
        """,
    ),
    Migration(
        version=4,
        label="checkpoints",
        sql="""
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            execution_id TEXT,
            plan_id TEXT,
            phase TEXT NOT NULL DEFAULT 'unknown',
            step_index INTEGER NOT NULL DEFAULT 0,
            state_ref TEXT,
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            trace_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_checkpoints_execution ON checkpoints (execution_id);
        """,
    ),
    Migration(
        version=5,
        label="continuations",
        sql="""
        CREATE TABLE IF NOT EXISTS continuations (
            continuation_id TEXT PRIMARY KEY,
            execution_id TEXT,
            checkpoint_id TEXT,
            continuation_of TEXT,
            mode TEXT NOT NULL DEFAULT 'advisory_only',
            resume_supported INTEGER NOT NULL DEFAULT 0,
            trace_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT
        );
        """,
    ),
    Migration(
        version=6,
        label="sagas",
        sql="""
        CREATE TABLE IF NOT EXISTS sagas (
            saga_id TEXT PRIMARY KEY,
            execution_id TEXT,
            current_step TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            steps_json TEXT NOT NULL DEFAULT '[]',
            state_json TEXT NOT NULL DEFAULT '{}',
            trace_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT,
            updated_at TEXT
        );
        """,
    ),
    Migration(
        version=7,
        label="resource_locks",
        sql="""
        CREATE TABLE IF NOT EXISTS resource_locks (
            lock_key TEXT NOT NULL,
            lock_type TEXT NOT NULL,
            owner TEXT NOT NULL,
            execution_id TEXT,
            context TEXT,
            ttl_seconds INTEGER NOT NULL DEFAULT 0,
            acquired_at TEXT,
            renewed_at TEXT,
            expires_at TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (lock_key, owner)
        );
        CREATE INDEX IF NOT EXISTS idx_resource_locks_expires ON resource_locks (expires_at);
        """,
    ),
    Migration(
        version=8,
        label="quota_profiles",
        sql="""
        CREATE TABLE IF NOT EXISTS quota_profiles (
            profile_id TEXT PRIMARY KEY,
            max_parallel_runs INTEGER NOT NULL DEFAULT 1,
            max_parallel_mutations_per_resource INTEGER NOT NULL DEFAULT 1,
            max_runtime_seconds INTEGER NOT NULL DEFAULT 7200,
            max_tool_calls INTEGER NOT NULL DEFAULT 256,
            max_tokens INTEGER NOT NULL DEFAULT 200000,
            priority INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        """,
    ),
    Migration(
        version=9,
        label="approvals",
        sql="""
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            resource TEXT,
            resource_fingerprint TEXT,
            principal TEXT,
            delegation_chain_sanitized TEXT NOT NULL DEFAULT '[]',
            decision TEXT NOT NULL DEFAULT 'requested',
            expires_at TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            consumed_at TEXT,
            metadata_sanitized TEXT NOT NULL DEFAULT '{}',
            approval_identity_assurance TEXT NOT NULL DEFAULT 'caller_asserted'
        );
        CREATE INDEX IF NOT EXISTS idx_approvals_decision_created_at
            ON approvals (decision, created_at);
        """,
    ),
]


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied."""


def _open_connection(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.isolation_level = None
    return connection


def _ensure_schema_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            label TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _current_version(connection: sqlite3.Connection) -> int:
    try:
        return int(
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            or 0
        )
    except sqlite3.OperationalError:
        return 0


def apply_migrations(db_path: str | None = None) -> int:
    """Apply pending migrations and return the current schema version."""
    settings = get_settings()
    target_db = db_path or settings.bridge_state_db_path
    connection = _open_connection(target_db)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            _ensure_schema_table(connection)
            current = _current_version(connection)
            for migration in _MIGRATIONS:
                if migration.version <= current:
                    continue
                connection.executescript(migration.sql)
                connection.execute(
                    "INSERT INTO schema_migrations (version, label, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.label, _utcnow().isoformat()),
                )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return _current_version(connection)
    finally:
        connection.close()


def reset_migrations(db_path: str | None = None) -> None:
    """Drop managed tables and reset the migration ledger."""
    settings = get_settings()
    target_db = db_path or settings.bridge_state_db_path
    connection = _open_connection(target_db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            tables = [
                "schema_migrations",
                "approvals",
                "quota_profiles",
                "resource_locks",
                "sagas",
                "continuations",
                "checkpoints",
                "plan_approvals",
                "plans",
                "run_mappings",
            ]
            for table in tables:
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


def _utcnow():
    from datetime import UTC, datetime
    return datetime.now(UTC)
