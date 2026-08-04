"""Schema migrations for the bridge state database."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings
from .observability.instrumentation import record_sqlite_operation


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
    Migration(
        version=10,
        label="run_mappings_updated_at_index",
        sql="""
        CREATE INDEX IF NOT EXISTS idx_run_mappings_updated_at
            ON run_mappings (updated_at);
        """,
    ),
]


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied."""


def _open_connection(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    connection.isolation_level = None
    # busy_timeout must be set before any statement that can take a lock
    # (journal_mode=WAL needs an exclusive lock on first switch), otherwise
    # concurrent migrators fail immediately with "database is locked".
    connection.execute("PRAGMA busy_timeout=30000")
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


def _ensure_wal(connection: sqlite3.Connection, *, attempts: int = 50) -> None:
    """Switch the database to WAL, tolerating concurrent migrators.

    ``PRAGMA journal_mode=WAL`` needs a brief exclusive lock and SQLite does
    **not** route that particular contention through the busy handler: it
    returns ``SQLITE_BUSY`` immediately. So a plain ``busy_timeout`` is not
    enough here and the pragma has to be retried explicitly. The loop is
    bounded (attempts x 10ms) and gives up by raising the original error, so it
    can never spin forever. Once any connection has switched the file to WAL
    the mode is persistent, so later calls return ``wal`` on the first try.
    """

    last_error: sqlite3.OperationalError | None = None
    for _ in range(max(1, attempts)):
        try:
            row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError as exc:  # pragma: no cover - timing dependent
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last_error = exc
        else:
            if row and str(row[0]).lower() == "wal":
                return
        time.sleep(0.01)
    if last_error is not None:
        raise last_error
    raise sqlite3.OperationalError("could not switch database to WAL journal mode")


def apply_migrations(db_path: str | None = None) -> int:
    """Apply pending migrations and return the current schema version."""
    settings = get_settings()
    target_db = db_path or settings.bridge_state_db_path
    if target_db and not target_db.startswith(":memory:"):
        Path(target_db).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    connection = _open_connection(target_db)
    try:
        _ensure_wal(connection)
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            # Serialise concurrent migrators: BEGIN IMMEDIATE takes the write
            # lock up-front so version checks and inserts cannot interleave
            # between processes/threads. busy_timeout above bounds the wait.
            connection.execute("BEGIN IMMEDIATE")
            try:
                _ensure_schema_table(connection)
                current = _current_version(connection)
                for migration in _MIGRATIONS:
                    if migration.version <= current:
                        continue
                    # executescript() commits the open transaction, so re-take
                    # the write lock right after to keep the ledger insert and
                    # the DDL under a single serialised migrator.
                    connection.executescript(migration.sql)
                    connection.execute(
                        "INSERT OR IGNORE INTO schema_migrations "
                        "(version, label, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.label, _utcnow().isoformat()),
                    )
                    connection.execute("BEGIN IMMEDIATE")
            finally:
                if connection.in_transaction:
                    connection.execute("COMMIT")
        except Exception as exc:
            record_sqlite_operation(kind="migrations", outcome="error", exc=exc)
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
