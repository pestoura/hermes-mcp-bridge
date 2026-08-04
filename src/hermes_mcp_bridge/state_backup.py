"""Online SQLite backup, restore and metadata helpers for bridge state.

Design guarantees (audit scope):
- backup uses the sqlite3 online backup API (consistent under WAL writers)
- restore is fail-closed: blocks on any active/unknown writer, prevents stale
  -wal/-shm sidecars from attaching to the restored database, and keeps an
  internal rollback bundle of the previous target (db + sidecars)
- all backup paths are absolute/canonical; default backups are unique
  (timestamp + nonce) and never overwrite silently
- metadata is sanitized (no rows, no secrets); includes online_backup flag
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import get_settings


class WriterState(StrEnum):
    """Deterministic classification of writer presence on a SQLite file."""

    CLEAR = "clear"
    ACTIVE = "active"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BackupMetadata:
    """Sanitized metadata for a state database backup."""

    timestamp_utc: str
    schema_migrations_count: int
    schema_migrations_version: int
    source_size_bytes: int
    backup_size_bytes: int
    source_sha256_prefix: str
    backup_sha256_prefix: str
    owner_uid: int
    mode: int
    bridge_version: str
    integrity_ok: bool = True
    online_backup: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "schema_migrations_count": self.schema_migrations_count,
            "schema_migrations_version": self.schema_migrations_version,
            "source_size_bytes": self.source_size_bytes,
            "backup_size_bytes": self.backup_size_bytes,
            "source_sha256_prefix": self.source_sha256_prefix,
            "backup_sha256_prefix": self.backup_sha256_prefix,
            "owner_uid": self.owner_uid,
            "mode": self.mode,
            "bridge_version": self.bridge_version,
            "integrity_ok": self.integrity_ok,
            "online_backup": self.online_backup,
        }


def _sha256_file(path: str, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _prefix(value: str, length: int = 16) -> str:
    return value[:length]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _bridge_version() -> str:
    try:
        return get_settings().bridge_version
    except Exception:
        return "unknown"


def _file_owner_mode(path: str) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_uid, st.st_mode & 0o777)


def _mode_of(path: str) -> int:
    return os.stat(path).st_mode & 0o777


def _current_migration_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.OperationalError:
        return 0


def _migration_count(connection: sqlite3.Connection) -> int:
    try:
        return int(
            connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        )
    except sqlite3.OperationalError:
        return 0


def _integrity_check(connection: sqlite3.Connection) -> bool:
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        return len(rows) == 1 and rows[0][0].lower() == "ok"
    except sqlite3.Error:
        return False


def _source_checks(path: str) -> None:
    if not path or path.startswith(":memory:"):
        raise ValueError(":memory: databases cannot be backed up online")
    if not Path(path).exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.isolation_level = None
    try:
        if not _integrity_check(conn):
            raise RuntimeError(f"source integrity_check failed for {path}")
    finally:
        conn.close()


def _fsync_file(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _detect_writer_state(path: str) -> WriterState:
    """Deterministic writer classification via SQLite locking.

    Returns CLEAR when an EXCLUSIVE lock is immediately obtained, ACTIVE when
    another writer holds the lock, and UNKNOWN on any other error. UNKNOWN is
    fail-closed for callers (treated like a writer present).
    """
    try:
        conn = sqlite3.connect(path, check_same_thread=False, timeout=0.1)
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA busy_timeout=0")
            try:
                conn.execute("BEGIN EXCLUSIVE")
                conn.execute("COMMIT")
                return WriterState.CLEAR
            except sqlite3.OperationalError:
                return WriterState.ACTIVE
        finally:
            conn.close()
    except sqlite3.Error:
        return WriterState.UNKNOWN
    except Exception:
        return WriterState.UNKNOWN


def _build_metadata(source: str, backup_path: str) -> BackupMetadata:
    src_conn = sqlite3.connect(source, check_same_thread=False)
    src_conn.isolation_level = None
    bkp_conn = sqlite3.connect(backup_path, check_same_thread=False)
    bkp_conn.isolation_level = None
    try:
        integrity_ok = _integrity_check(src_conn) and _integrity_check(bkp_conn)
        return BackupMetadata(
            timestamp_utc=_utc_now(),
            schema_migrations_count=_migration_count(src_conn),
            schema_migrations_version=_current_migration_version(src_conn),
            source_size_bytes=Path(source).stat().st_size,
            backup_size_bytes=Path(backup_path).stat().st_size,
            source_sha256_prefix=_prefix(_sha256_file(source)),
            backup_sha256_prefix=_prefix(_sha256_file(backup_path)),
            owner_uid=_file_owner_mode(source)[0],
            mode=_mode_of(backup_path),
            bridge_version=_bridge_version(),
            integrity_ok=integrity_ok,
            online_backup=True,
        )
    finally:
        src_conn.close()
        bkp_conn.close()


def _preview_metadata(source: str) -> BackupMetadata:
    conn = sqlite3.connect(source, check_same_thread=False)
    conn.isolation_level = None
    try:
        return BackupMetadata(
            timestamp_utc=_utc_now(),
            schema_migrations_count=_migration_count(conn),
            schema_migrations_version=_current_migration_version(conn),
            source_size_bytes=Path(source).stat().st_size,
            backup_size_bytes=0,
            source_sha256_prefix=_prefix(_sha256_file(source)),
            backup_sha256_prefix="",
            owner_uid=_file_owner_mode(source)[0],
            mode=0o600,
            bridge_version=_bridge_version(),
            integrity_ok=_integrity_check(conn),
            online_backup=True,
        )
    finally:
        conn.close()


def _enforce_retention(source: str, retention_count: int) -> list[str]:
    candidates = sorted(
        [str(p) for p in Path(source).parent.glob(Path(source).name + ".backup-*")],
        key=lambda p: Path(p).stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for old in candidates[retention_count:]:
        Path(old).unlink(missing_ok=True)
        removed.append(old)
    return removed


def backup_state_db(
    source_path: str | None = None,
    backup_path: str | None = None,
    *,
    retention_count: int | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create an online SQLite backup with atomic output and sanitized metadata.

    Returns a result dict with status, metadata and paths. Default backups are
    unique (UTC timestamp + nonce). An explicit backup_path that already exists
    is fail-closed unless overwrite=True.
    """
    settings = get_settings()
    source = source_path or settings.bridge_state_db_path
    if not source:
        raise RuntimeError("bridge state db path is not configured")
    source = os.path.abspath(source)

    if backup_path:
        base_backup = os.path.abspath(backup_path)
        if Path(base_backup).exists() and not overwrite:
            raise FileExistsError(
                f"backup target already exists: {base_backup} (use overwrite=True)"
            )
    else:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        nonce = uuid.uuid4().hex[:8]
        base_backup = f"{source}.backup-{ts}-{nonce}"

    _source_checks(source)

    if dry_run:
        return {
            "status": "dry_run",
            "source": source,
            "backup": base_backup,
            "metadata": _preview_metadata(source).as_dict(),
        }

    tmp_dir = os.path.dirname(base_backup)
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=tmp_dir, prefix=".backup-", suffix=".sqlite3"
    )
    os.close(tmp_fd)
    removed: list[str] = []
    try:
        source_conn = sqlite3.connect(source, check_same_thread=False)
        source_conn.isolation_level = None
        try:
            source_conn.execute("PRAGMA busy_timeout=5000")
            backup_conn = sqlite3.connect(tmp_path, check_same_thread=False)
            backup_conn.isolation_level = None
            backup_conn.execute("PRAGMA journal_mode=WAL")
            backup_conn.execute("PRAGMA synchronous=NORMAL")
            try:
                source_conn.backup(backup_conn)
                backup_conn.execute("PRAGMA wal_checkpoint=FULL")
                backup_conn.execute("PRAGMA journal_mode=DELETE")
            finally:
                backup_conn.close()
        finally:
            source_conn.close()

        _fsync_file(tmp_path)
        meta = _build_metadata(source, tmp_path)
        Path(tmp_path).chmod(0o600)
        os.replace(tmp_path, base_backup)
        Path(base_backup).chmod(0o600)
        _fsync_file(base_backup)
        _fsync_dir(Path(base_backup).parent)

        if retention_count is not None and retention_count >= 0 and not backup_path:
            removed = _enforce_retention(source, retention_count)

        return {
            "status": "ok",
            "source": source,
            "backup": base_backup,
            "removed_retained": removed,
            "metadata": meta.as_dict(),
        }
    except BaseException:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink(missing_ok=True)
        raise


def _sidecar_suffixes() -> list[str]:
    return ["", "-wal", "-shm"]


def _restore_bundle_path(target: str, ts: str, nonce: str) -> str:
    return f"{target}.pre-restore-{ts}-{nonce}"


def restore_state_db(
    backup_path: str,
    target_path: str | None = None,
    *,
    require_bridge_stopped: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Restore a state database from a trusted backup.

    Fail-closed: blocks on any active/unknown writer unless force=True. Prevents
    stale -wal/-shm sidecars from attaching to the restored DB, preserves an
    internal rollback bundle of the previous target (db + sidecars), and restores
    it on any failure.
    """
    settings = get_settings()
    target = target_path or settings.bridge_state_db_path
    if not target:
        raise RuntimeError("bridge state db path is not configured")
    target = os.path.abspath(target)
    backup_path = os.path.abspath(backup_path)

    if not Path(backup_path).exists():
        raise FileNotFoundError(backup_path)
    if backup_path.startswith(":memory:") or target.startswith(":memory:"):
        raise ValueError("restore does not support :memory: databases")

    writer_state = (
        _detect_writer_state(target) if Path(target).exists() else WriterState.CLEAR
    )
    if writer_state != WriterState.CLEAR and not force:
        raise RuntimeError(
            f"target writer state={writer_state.value}; use force=True to override"
        )
    if require_bridge_stopped and writer_state != WriterState.CLEAR:
        raise RuntimeError(
            f"bridge appears active (writer state={writer_state.value}); stop it before restore"
        )

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    nonce = uuid.uuid4().hex[:8]
    bundle_base = _restore_bundle_path(target, ts, nonce)
    bundle: dict[str, str] = {}

    # Move the current target and its sidecars into a rollback bundle.
    for suffix in _sidecar_suffixes():
        orig = target + suffix
        if Path(orig).exists():
            bk = bundle_base + suffix
            Path(orig).replace(bk)
            Path(bk).chmod(0o600)
            bundle[suffix] = bk

    # Remove any stale sidecars that may have been left without a db.
    for suffix in ("-wal", "-shm"):
        stale = target + suffix
        if Path(stale).exists():
            Path(stale).unlink(missing_ok=True)

    tmp_dir = os.path.dirname(target)
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=tmp_dir, prefix=".restore-", suffix=".sqlite3"
    )
    os.close(tmp_fd)
    try:
        bkp_conn = sqlite3.connect(backup_path, check_same_thread=False)
        bkp_conn.isolation_level = None
        try:
            if not _integrity_check(bkp_conn):
                raise RuntimeError(f"backup integrity_check failed for {backup_path}")
            dst_conn = sqlite3.connect(tmp_path, check_same_thread=False)
            dst_conn.isolation_level = None
            dst_conn.execute("PRAGMA journal_mode=WAL")
            dst_conn.execute("PRAGMA synchronous=NORMAL")
            try:
                bkp_conn.backup(dst_conn)
                dst_conn.execute("PRAGMA wal_checkpoint=FULL")
                dst_conn.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst_conn.close()
        finally:
            bkp_conn.close()

        _fsync_file(tmp_path)
        Path(tmp_path).chmod(0o600)
        os.replace(tmp_path, target)
        Path(target).chmod(0o600)
        # Ensure no stale sidecars attach to the freshly restored DB.
        for suffix in ("-wal", "-shm"):
            stale = target + suffix
            if Path(stale).exists():
                Path(stale).unlink(missing_ok=True)
        _fsync_file(target)
        _fsync_dir(Path(target).parent)
        return {
            "status": "ok",
            "backup": backup_path,
            "target": target,
            "previous_target_backup": bundle_base,
            "metadata": _build_metadata(target, backup_path).as_dict(),
        }
    except BaseException:
        # Internal rollback: restore the previous target bundle.
        if Path(tmp_path).exists():
            Path(tmp_path).unlink(missing_ok=True)
        for suffix in _sidecar_suffixes():
            bk = bundle.get(suffix)
            orig = target + suffix
            if bk and Path(bk).exists():
                Path(orig).unlink(missing_ok=True)
                Path(bk).replace(orig)
                Path(orig).chmod(0o600)
        raise


def verify_backup(backup_path: str, source_path: str | None = None) -> dict[str, Any]:
    """Verify backup integrity and compatibility without restoring."""
    settings = get_settings()
    source = source_path or settings.bridge_state_db_path
    if source:
        source = os.path.abspath(source)
    if not Path(backup_path).exists():
        raise FileNotFoundError(backup_path)
    bkp_conn = sqlite3.connect(backup_path, check_same_thread=False)
    bkp_conn.isolation_level = None
    try:
        bkp_integrity = _integrity_check(bkp_conn)
        bkp_version = _current_migration_version(bkp_conn)
        bkp_count = _migration_count(bkp_conn)
    finally:
        bkp_conn.close()

    src_integrity = None
    src_version = None
    if source and Path(source).exists():
        src_conn = sqlite3.connect(source, check_same_thread=False)
        src_conn.isolation_level = None
        try:
            src_integrity = _integrity_check(src_conn)
            src_version = _current_migration_version(src_conn)
        finally:
            src_conn.close()

    compatible = True
    if src_version is not None and bkp_version > src_version:
        compatible = False
    return {
        "backup_integrity": bkp_integrity,
        "backup_schema_version": bkp_version,
        "backup_migrations_count": bkp_count,
        "source_integrity": src_integrity,
        "source_schema_version": src_version,
        "compatible": compatible,
        "online_backup": True,
    }
