"""Read-only state diagnostics and explicitly gated SQLite maintenance.

The diagnostic path opens SQLite with ``mode=ro`` and ``query_only=ON``. It
returns only aggregate operational metadata: no identifiers, fingerprints,
prompts, results, resources, owners or secret-bearing values.

Checkpoint execution is a separate, opt-in mutation. It defaults to a dry plan,
requires a clear writer state and uses the same process-exclusion lock as the
backup/restore helpers.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ._file_lock import FileLockError, exclusive_file_lock
from .config import get_settings
from .models import TERMINAL_STATUSES, RunStatus
from .state_backup import (
    ALLOWED_ROOT_ENV,
    LOCK_PATH,
    WriterState,
    _canonical_db_path,
    _current_migration_version,
    _detect_writer_state,
    _integrity_check,
    _migration_count,
    restore_state_db,
)

DEFAULT_STALE_AFTER_SECONDS = 60 * 60
DEFAULT_WAL_WARNING_BYTES = 64 * 1024 * 1024
DEFAULT_DB_WARNING_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_FREE_WARNING_BYTES = 5 * 1024 * 1024 * 1024

KNOWN_TABLES = (
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
)

CHECKPOINT_MODES = frozenset({"PASSIVE", "FULL", "RESTART", "TRUNCATE"})


@dataclass(frozen=True)
class StateThresholds:
    """Bounded warning thresholds for aggregate state diagnostics."""

    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS
    wal_warning_bytes: int = DEFAULT_WAL_WARNING_BYTES
    db_warning_bytes: int = DEFAULT_DB_WARNING_BYTES
    free_warning_bytes: int = DEFAULT_FREE_WARNING_BYTES

    def __post_init__(self) -> None:
        if self.stale_after_seconds < 1:
            raise ValueError("stale_after_seconds must be positive")
        for field_name in (
            "wal_warning_bytes",
            "db_warning_bytes",
            "free_warning_bytes",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must not be negative")


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _read_only_connection(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve(strict=True).as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
    connection.isolation_level = None
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    if table not in KNOWN_TABLES or not _table_exists(connection, table):
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _status_counts(connection: sqlite3.Connection) -> dict[str, int]:
    allowed = tuple(status.value for status in RunStatus)
    counts = {status: 0 for status in allowed}
    counts["other"] = 0
    if not _table_exists(connection, "run_mappings"):
        return counts
    rows = connection.execute(
        "SELECT last_status, COUNT(*) FROM run_mappings GROUP BY last_status"
    ).fetchall()
    for status, count in rows:
        normalized = str(status or "").lower()
        bucket = normalized if normalized in counts and normalized != "other" else "other"
        counts[bucket] += int(count)
    return counts


def _stale_run_counts(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    stale_after_seconds: int,
) -> dict[str, int]:
    if not _table_exists(connection, "run_mappings"):
        return {"non_terminal": 0, "stale": 0, "timestamp_invalid": 0}

    terminal = {status.value for status in TERMINAL_STATUSES}
    non_terminal = 0
    stale = 0
    timestamp_invalid = 0
    for status, updated_at in connection.execute(
        "SELECT last_status, updated_at FROM run_mappings"
    ):
        if str(status) in terminal:
            continue
        non_terminal += 1
        parsed = _parse_timestamp(updated_at)
        if parsed is None:
            timestamp_invalid += 1
            continue
        if (now - parsed).total_seconds() >= stale_after_seconds:
            stale += 1
    return {
        "non_terminal": non_terminal,
        "stale": stale,
        "timestamp_invalid": timestamp_invalid,
    }


def _lock_counts(connection: sqlite3.Connection, *, now: datetime) -> dict[str, int]:
    if not _table_exists(connection, "resource_locks"):
        return {
            "active": 0,
            "active_expired": 0,
            "active_without_expiry": 0,
            "released": 0,
            "expired_status": 0,
            "timestamp_invalid": 0,
        }

    counts = {
        "active": 0,
        "active_expired": 0,
        "active_without_expiry": 0,
        "released": 0,
        "expired_status": 0,
        "timestamp_invalid": 0,
    }
    for status, expires_at in connection.execute(
        "SELECT status, expires_at FROM resource_locks"
    ):
        normalized = str(status or "unknown").lower()
        if normalized == "released":
            counts["released"] += 1
            continue
        if normalized == "expired":
            counts["expired_status"] += 1
            continue
        if normalized != "active":
            continue
        counts["active"] += 1
        if expires_at is None or not str(expires_at).strip():
            counts["active_without_expiry"] += 1
            continue
        parsed = _parse_timestamp(expires_at)
        if parsed is None:
            counts["timestamp_invalid"] += 1
        elif parsed <= now:
            counts["active_expired"] += 1
    return counts


def _approval_counts(connection: sqlite3.Connection, *, now: datetime) -> dict[str, int]:
    if not _table_exists(connection, "approvals"):
        return {"pending": 0, "pending_expired": 0, "timestamp_invalid": 0}

    counts = {"pending": 0, "pending_expired": 0, "timestamp_invalid": 0}
    for decision, expires_at in connection.execute(
        "SELECT decision, expires_at FROM approvals"
    ):
        if str(decision or "").lower() not in {"requested", "approved"}:
            continue
        counts["pending"] += 1
        if expires_at is None or not str(expires_at).strip():
            continue
        parsed = _parse_timestamp(expires_at)
        if parsed is None:
            counts["timestamp_invalid"] += 1
        elif parsed <= now:
            counts["pending_expired"] += 1
    return counts


def _saga_counts(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    stale_after_seconds: int,
) -> dict[str, int]:
    if not _table_exists(connection, "sagas"):
        return {"running": 0, "stale": 0, "timestamp_invalid": 0}

    counts = {"running": 0, "stale": 0, "timestamp_invalid": 0}
    for status, updated_at, created_at in connection.execute(
        "SELECT status, updated_at, created_at FROM sagas"
    ):
        if str(status or "").lower() not in {"running", "compensating"}:
            continue
        counts["running"] += 1
        parsed = _parse_timestamp(updated_at) or _parse_timestamp(created_at)
        if parsed is None:
            counts["timestamp_invalid"] += 1
        elif (now - parsed).total_seconds() >= stale_after_seconds:
            counts["stale"] += 1
    return counts


def _file_size(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except FileNotFoundError:
        return 0


def diagnose_state_db(
    db_path: str | None = None,
    *,
    now: datetime | None = None,
    thresholds: StateThresholds | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return aggregate, read-only operational diagnostics for bridge state."""

    effective_thresholds = thresholds or StateThresholds()
    current_time = _utc_now(now)
    settings = get_settings()
    candidate = db_path or settings.bridge_state_db_path
    environ = env if env is not None else os.environ
    canonical = _canonical_db_path(
        candidate,
        allowed_root=environ.get(ALLOWED_ROOT_ENV),
    )
    if not Path(canonical).exists():
        raise FileNotFoundError(canonical)

    stat = Path(canonical).stat()
    disk = shutil.disk_usage(Path(canonical).parent)
    db_bytes = stat.st_size
    wal_bytes = _file_size(canonical + "-wal")
    shm_bytes = _file_size(canonical + "-shm")
    warnings: list[str] = []

    if db_bytes >= effective_thresholds.db_warning_bytes:
        warnings.append("database_size")
    if wal_bytes >= effective_thresholds.wal_warning_bytes:
        warnings.append("wal_size")
    if disk.free <= effective_thresholds.free_warning_bytes:
        warnings.append("disk_free")

    connection = _read_only_connection(canonical)
    try:
        quick_rows = connection.execute("PRAGMA quick_check").fetchall()
        quick_ok = len(quick_rows) == 1 and str(quick_rows[0][0]).lower() == "ok"
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        wal_autocheckpoint = int(
            connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        )
        synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        migration_version = _current_migration_version(connection)
        migration_count = _migration_count(connection)
        row_counts = {table: _row_count(connection, table) for table in KNOWN_TABLES}
        run_statuses = _status_counts(connection)
        stale_runs = _stale_run_counts(
            connection,
            now=current_time,
            stale_after_seconds=effective_thresholds.stale_after_seconds,
        )
        locks = _lock_counts(connection, now=current_time)
        approvals = _approval_counts(connection, now=current_time)
        sagas = _saga_counts(
            connection,
            now=current_time,
            stale_after_seconds=effective_thresholds.stale_after_seconds,
        )
    finally:
        connection.close()

    if not quick_ok:
        warnings.append("quick_check")
    if stale_runs["stale"]:
        warnings.append("stale_runs")
    if locks["active_expired"] or locks["timestamp_invalid"]:
        warnings.append("stale_locks")
    if approvals["pending_expired"]:
        warnings.append("expired_approvals")
    if sagas["stale"]:
        warnings.append("stale_sagas")

    return {
        "status": "ready" if not warnings else "attention",
        "read_only": True,
        "quick_check_ok": quick_ok,
        "schema": {
            "migration_version": migration_version,
            "migration_count": migration_count,
        },
        "sqlite": {
            "journal_mode": journal_mode,
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "wal_autocheckpoint": wal_autocheckpoint,
            "synchronous": synchronous,
        },
        "storage": {
            "database_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "shm_bytes": shm_bytes,
            "state_total_bytes": db_bytes + wal_bytes + shm_bytes,
            "disk_free_bytes": disk.free,
            "owner_uid": stat.st_uid,
            "mode": stat.st_mode & 0o777,
        },
        "tables": row_counts,
        "runs": {"status_counts": run_statuses, **stale_runs},
        "locks": locks,
        "approvals": approvals,
        "sagas": sagas,
        "checkpoint": {
            "recommended": wal_bytes >= effective_thresholds.wal_warning_bytes,
            "reason": "wal_size"
            if wal_bytes >= effective_thresholds.wal_warning_bytes
            else None,
        },
        "warnings": sorted(set(warnings)),
    }


def checkpoint_state_db(
    db_path: str | None = None,
    *,
    mode: str = "TRUNCATE",
    execute: bool = False,
    require_writer_clear: bool = True,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Plan or execute one controlled WAL checkpoint.

    ``execute`` defaults to ``False``. Execution never bypasses path safety and
    fails closed on active/unknown writers unless ``require_writer_clear`` is
    explicitly disabled by an operator-controlled caller.
    """

    normalized_mode = str(mode).strip().upper()
    if normalized_mode not in CHECKPOINT_MODES:
        raise ValueError("checkpoint mode must be PASSIVE/FULL/RESTART/TRUNCATE")

    settings = get_settings()
    candidate = db_path or settings.bridge_state_db_path
    environ = env if env is not None else os.environ
    canonical = _canonical_db_path(
        candidate,
        allowed_root=environ.get(ALLOWED_ROOT_ENV),
    )
    if not Path(canonical).exists():
        raise FileNotFoundError(canonical)

    writer_state = _detect_writer_state(canonical)
    before_wal_bytes = _file_size(canonical + "-wal")
    plan = {
        "status": "dry_run" if not execute else "pending",
        "mode": normalized_mode,
        "execute": execute,
        "writer_state": writer_state.value,
        "wal_bytes_before": before_wal_bytes,
    }
    if not execute:
        return plan
    if require_writer_clear and writer_state != WriterState.CLEAR:
        raise RuntimeError(f"state writer state={writer_state.value}; checkpoint blocked")

    try:
        with exclusive_file_lock(LOCK_PATH):
            connection = sqlite3.connect(canonical, check_same_thread=False, timeout=5.0)
            connection.isolation_level = None
            try:
                connection.execute("PRAGMA busy_timeout=5000")
                row = connection.execute(
                    f"PRAGMA wal_checkpoint({normalized_mode})"
                ).fetchone()
            finally:
                connection.close()
    except FileLockError as exc:
        raise RuntimeError(f"state maintenance lock unavailable: {exc}") from exc

    busy, log_pages, checkpointed_pages = (int(value) for value in (row or (1, 0, 0)))
    return {
        "status": "ok" if busy == 0 else "busy",
        "mode": normalized_mode,
        "execute": True,
        "writer_state": writer_state.value,
        "busy": busy,
        "log_pages": log_pages,
        "checkpointed_pages": checkpointed_pages,
        "wal_bytes_before": before_wal_bytes,
        "wal_bytes_after": _file_size(canonical + "-wal"),
    }


def verify_restore_in_isolation(
    backup_path: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Exercise the real restore path in an isolated temporary target."""

    environ = env if env is not None else os.environ
    allowed_root = environ.get(ALLOWED_ROOT_ENV)
    backup = _canonical_db_path(backup_path, allowed_root=allowed_root)
    if not Path(backup).exists():
        raise FileNotFoundError(backup)

    parent = Path(backup).parent
    with TemporaryDirectory(prefix=".restore-proof-", dir=parent) as temp_dir:
        target = str(Path(temp_dir) / "state.sqlite3")
        result = restore_state_db(
            backup,
            target,
            require_bridge_stopped=True,
            force=False,
        )
        connection = _read_only_connection(target)
        try:
            integrity_ok = _integrity_check(connection)
            migration_version = _current_migration_version(connection)
            migration_count = _migration_count(connection)
            table_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            )
        finally:
            connection.close()

        target_bytes = _file_size(target)
        restored_status = str(result.get("status"))

    # TemporaryDirectory proves cleanup by removing the target and any sidecars.
    return {
        "status": "pass" if integrity_ok and restored_status == "ok" else "fail",
        "isolated_restore": True,
        "cleanup_ok": not Path(temp_dir).exists(),
        "integrity_ok": integrity_ok,
        "migration_version": migration_version,
        "migration_count": migration_count,
        "table_count": table_count,
        "restored_bytes": target_bytes,
    }


def safe_diagnostic_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the fixed public subset used by runbooks and future MCP tools."""

    keys = (
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
    )
    return {key: payload[key] for key in keys if key in payload}


def close_quietly(connection: sqlite3.Connection | None) -> None:
    """Small helper for callers that need cleanup without masking diagnostics."""

    if connection is not None:
        with suppress(Exception):
            connection.close()
