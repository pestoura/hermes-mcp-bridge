"""Online SQLite backup, restore and metadata helpers for bridge state."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import get_settings

_SETTINGS = get_settings()


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


def _file_owner_mode(path: str) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_uid, st.st_mode & 0o777)


def _current_migration_version(connection: sqlite3.Connection) -> int:
    try:
        return int(
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            or 0
        )
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


def _acquire_backup_lock(connection: sqlite3.Connection) -> sqlite3.Connection:
    return connection


def backup_state_db(
    source_path: str | None = None,
    backup_path: str | None = None,
    *,
    retention_count: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create an online SQLite backup with atomic output and sanitized metadata.

    Returns a result dict with status, metadata, and paths.
    """
    settings = get_settings()
    source = source_path or settings.bridge_state_db_path
    if not source:
        raise RuntimeError("bridge state db path is not configured")

    base_backup = backup_path or f"{source}.backup"
    _source_checks(source)

    tmp_dir = os.path.dirname(os.path.abspath(base_backup))
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=tmp_dir, prefix=".backup-", suffix=".sqlite3"
    )
    os.close(tmp_fd)
    try:
        if dry_run:
            return {
                "status": "dry_run",
                "source": source,
                "backup": base_backup,
                "temporary_backup": tmp_path,
                "metadata": _preview_metadata(source).as_dict(),
            }

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

        meta = _build_metadata(source, tmp_path)
        Path(tmp_path).chmod(0o600)
        os.replace(tmp_path, base_backup)
        Path(base_backup).chmod(0o600)
        Path(base_backup).parent.mkdir(parents=True, exist_ok=True)

        if retention_count is not None and retention_count >= 0:
            _enforce_retention(base_backup, retention_count)

        return {
            "status": "ok",
            "source": source,
            "backup": base_backup,
            "metadata": meta.as_dict(),
        }
    except BaseException:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink(missing_ok=True)
        raise


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
            bridge_version=_SETTINGS.bridge_version,
            integrity_ok=_integrity_check(conn),
        )
    finally:
        conn.close()


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
            mode=0o600,
            bridge_version=_SETTINGS.bridge_version,
            integrity_ok=integrity_ok,
        )
    finally:
        src_conn.close()
        bkp_conn.close()


def _enforce_retention(base_backup: str, retention_count: int) -> None:
    candidates = sorted(
        [p for p in Path(base_backup).parent.glob(Path(base_backup).name + ".*")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in candidates[retention_count:]:
        old.unlink(missing_ok=True)


def _active_writer_pid(path: str) -> int | None:
    """Return PID with an active SQLite writer lock, if detectable.

    Fail-closed: if the connection cannot obtain an EXCLUSIVE lock because
    another writer holds it, return -1 (unknown but present writer) instead of
    assuming the database is idle.
    """
    try:
        conn = sqlite3.connect(path, check_same_thread=False, timeout=0.1)
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA busy_timeout=10")
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("COMMIT")
            return None
        except sqlite3.OperationalError:
            return _pid_from_wal(path) or -1
        finally:
            conn.close()
    except Exception:
        return None


def _pid_from_wal(path: str) -> int | None:
    wal = path + "-shm"
    if not Path(wal).exists():
        return None
    try:
        data = Path(wal).read_bytes()
        tokens = data.split(b"\x00")
        for token in tokens:
            text = token.decode("utf-8", errors="ignore")
            if text.isdigit():
                pid = int(text)
                if _pid_alive(pid):
                    return pid
    except Exception:
        pass
    return None


def _pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def restore_state_db(
    backup_path: str,
    target_path: str | None = None,
    *,
    require_bridge_stopped: bool = False,
    bridge_pid_path: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Restore a state database from a trusted backup.

    Fails closed when writers are detected unless force=True.
    """
    settings = get_settings()
    target = target_path or settings.bridge_state_db_path
    if not target:
        raise RuntimeError("bridge state db path is not configured")
    if not Path(backup_path).exists():
        raise FileNotFoundError(backup_path)
    if backup_path.startswith(":memory:") or target.startswith(":memory:"):
        raise ValueError("restore does not support :memory: databases")

    writer_pid = _active_writer_pid(target) if Path(target).exists() else None
    if writer_pid is not None and not force:
        raise RuntimeError(
            f"target database has active writer pid={writer_pid}; use force=True to override"
        )
    if require_bridge_stopped and writer_pid is not None:
        raise RuntimeError(
            f"bridge appears active (writer pid={writer_pid}); stop it before restore"
        )

    current_backup = target + ".pre-restore-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    if Path(target).exists():
        Path(target).replace(current_backup)
        Path(current_backup).chmod(0o600)

    tmp_dir = os.path.dirname(os.path.abspath(target))
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

        if Path(target).exists():
            Path(target).unlink(missing_ok=True)
        Path(tmp_path).replace(target)
        Path(target).chmod(0o600)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        _fsync_dir(Path(target).parent)
        return {
            "status": "ok",
            "backup": backup_path,
            "target": target,
            "previous_target_backup": current_backup,
            "metadata": _build_metadata(target, backup_path).as_dict(),
        }
    except BaseException:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink(missing_ok=True)
        if not Path(target).exists() and Path(current_backup).exists():
            Path(current_backup).replace(target)
            Path(target).chmod(0o600)
        raise


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_backup(backup_path: str, source_path: str | None = None) -> dict[str, Any]:
    """Verify backup integrity and compatibility without restoring."""
    settings = get_settings()
    source = source_path or settings.bridge_state_db_path
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
    }
