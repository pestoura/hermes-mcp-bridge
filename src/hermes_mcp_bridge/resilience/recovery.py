"""Post-crash state recovery for the bridge.

After an abrupt restart the bridge must be able to answer "what was running?"
from the SQLite state alone, without resubmitting anything upstream. This
module reads the run registry, reports recoverable runs and reaps locks that
belong to a previous process generation.

It never calls the Hermes API and never mutates ``run_mappings`` status.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .events import fingerprint

NON_TERMINAL_STATUSES: frozenset[str] = frozenset({"queued", "running", "unknown"})


@dataclass
class RecoveryReport:
    """Sanitized recovery summary: fingerprints only, never raw prompts."""

    schema_version: int = 0
    recoverable_runs: int = 0
    terminal_runs: int = 0
    locks_reaped: int = 0
    inflight_cleared: int = 0
    runs: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "recoverable_runs": self.recoverable_runs,
            "terminal_runs": self.terminal_runs,
            "locks_reaped": self.locks_reaped,
            "inflight_cleared": self.inflight_cleared,
            "runs": self.runs,
        }


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.isolation_level = None
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    cursor = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    )
    return cursor.fetchone() is not None


def recover_state(db_path: str, *, reap_locks: bool = True, limit: int = 500) -> RecoveryReport:
    """Inspect persisted state after a restart and return a sanitized report.

    ``execution_id`` values are preserved in full only inside SQLite; the report
    carries fingerprints so it can be logged or shipped safely.
    """

    report = RecoveryReport()
    bounded = max(1, min(5000, int(limit)))
    connection = _connect(db_path)
    try:
        if _table_exists(connection, "schema_migrations"):
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            report.schema_version = int(row[0] or 0)

        if _table_exists(connection, "run_mappings"):
            cursor = connection.execute(
                "SELECT client_request_id, execution_id, last_status"
                " FROM run_mappings ORDER BY updated_at DESC LIMIT ?",
                (bounded,),
            )
            for client_request_id, execution_id, last_status in cursor.fetchall():
                status = str(last_status or "unknown").lower()
                if status in NON_TERMINAL_STATUSES:
                    report.recoverable_runs += 1
                    report.runs.append(
                        {
                            "client_request_fp": fingerprint(client_request_id),
                            "execution_fp": fingerprint(execution_id),
                            "status": status,
                        }
                    )
                else:
                    report.terminal_runs += 1

        if reap_locks and _table_exists(connection, "resource_locks"):
            now = datetime.now(UTC).isoformat()
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE resource_locks SET status = 'expired'"
                    " WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
                    (now,),
                )
                report.locks_reaped = int(cursor.rowcount or 0)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
    finally:
        connection.close()

    report.inflight_cleared = _reset_inflight_gauges()
    return report


def _reset_inflight_gauges() -> int:
    """Zero in-flight gauges left over from a crashed generation."""

    from ..observability.metrics import get_metrics

    metric = getattr(get_metrics(), "tool_inflight", None)
    values = getattr(metric, "_values", None)
    if not isinstance(values, dict):
        return 0
    cleared = 0
    for key in list(values):
        if values[key] != 0.0:
            cleared += 1
        values[key] = 0.0
    return cleared


def lookup_execution(db_path: str, client_request_id: str) -> dict[str, str] | None:
    """Resolve a client_request_id to its persisted run without resubmitting."""

    connection = _connect(db_path)
    try:
        if not _table_exists(connection, "run_mappings"):
            return None
        row = connection.execute(
            "SELECT execution_id, session_id, last_status FROM run_mappings"
            " WHERE client_request_id = ?",
            (client_request_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "execution_id": str(row[0]),
            "session_id": str(row[1] or ""),
            "last_status": str(row[2]),
        }
    finally:
        connection.close()
