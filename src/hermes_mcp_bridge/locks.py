"""Resource lock registry with SQLite-backed TTL and compatibility rules."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import LockStatus, LockType, ResourceLock


class LockError(RuntimeError):
    """Raised when a lock cannot be acquired or released."""


_INCOMPATIBLE_LOCK_PAIRS: set[tuple[str, str]] = {
    (LockType.WRITE_EXCLUSIVE.value, LockType.READ_SHARED.value),
    (LockType.WRITE_EXCLUSIVE.value, LockType.WRITE_EXCLUSIVE.value),
    (LockType.WRITE_EXCLUSIVE.value, LockType.INTENT_TO_WRITE.value),
    (LockType.WRITE_EXCLUSIVE.value, LockType.APPROVAL_PENDING.value),
    (LockType.INTENT_TO_WRITE.value, LockType.WRITE_EXCLUSIVE.value),
    (LockType.APPROVAL_PENDING.value, LockType.WRITE_EXCLUSIVE.value),
}


class LockRegistry:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._global_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        from .migrations import apply_migrations
        apply_migrations(self._db_path)
        self._initialized = True

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def acquire(self, lock_request: ResourceLock) -> ResourceLock:
        with self._global_lock:
            connection = sqlite3.connect(self._db_path, check_same_thread=False)
            connection.isolation_level = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._enforce_compatibility(connection, lock_request)
                    self._reap_expired(connection)
                    now = self._now()
                    expires = self._expires_at(now, lock_request.ttl_seconds)
                    connection.execute(
                        """
                        INSERT INTO resource_locks (
                            lock_key, lock_type, owner, execution_id, context,
                            ttl_seconds, acquired_at, renewed_at, expires_at, status, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(lock_key, owner) DO UPDATE SET
                            lock_type=excluded.lock_type,
                            execution_id=excluded.execution_id,
                            context=excluded.context,
                            ttl_seconds=excluded.ttl_seconds,
                            renewed_at=excluded.acquired_at,
                            expires_at=excluded.expires_at,
                            status=excluded.status,
                            metadata_json=excluded.metadata_json
                        """,
                        (
                            lock_request.lock_key,
                            lock_request.lock_type.value,
                            lock_request.owner,
                            lock_request.execution_id,
                            lock_request.context,
                            lock_request.ttl_seconds,
                            now,
                            now,
                            expires,
                            LockStatus.ACTIVE.value,
                            json.dumps(
                                lock_request.metadata,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
        return self.get(lock_request.lock_key, lock_request.owner)

    def release(self, lock_key: str, owner: str) -> ResourceLock | None:
        with self._global_lock:
            connection = sqlite3.connect(self._db_path, check_same_thread=False)
            connection.isolation_level = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "SELECT lock_type, execution_id, context, ttl_seconds, "
                    "acquired_at, renewed_at, expires_at, status, metadata_json "
                    "FROM resource_locks WHERE lock_key = ? AND owner = ?",
                    (lock_key, owner),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return None
                connection.execute(
                    "UPDATE resource_locks SET status = ?, renewed_at = ? "
                    "WHERE lock_key = ? AND owner = ?",
                    (LockStatus.RELEASED.value, self._now(), lock_key, owner),
                )
                connection.execute("COMMIT")
                return self._row_to_lock(lock_key, owner, row)
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def get(self, lock_key: str, owner: str) -> ResourceLock | None:
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.isolation_level = None
        try:
            cursor = connection.execute(
                "SELECT lock_type, execution_id, context, ttl_seconds, "
                "acquired_at, renewed_at, expires_at, status, metadata_json "
                "FROM resource_locks WHERE lock_key = ? AND owner = ?",
                (lock_key, owner),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_lock(lock_key, owner, row)
        finally:
            connection.close()

    def list_status(self, lock_key: str | None = None) -> list[dict[str, Any]]:
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.isolation_level = None
        try:
            if lock_key is None:
                cursor = connection.execute(
                    "SELECT lock_key, lock_type, owner, execution_id, context, "
                    "ttl_seconds, acquired_at, renewed_at, expires_at, status "
                    "FROM resource_locks"
                )
            else:
                cursor = connection.execute(
                    "SELECT lock_key, lock_type, owner, execution_id, context, "
                    "ttl_seconds, acquired_at, renewed_at, expires_at, status "
                    "FROM resource_locks WHERE lock_key = ?",
                    (lock_key,),
                )
            rows = cursor.fetchall()
            return [
                {
                    "lock_key": row[0],
                    "lock_type": row[1],
                    "owner": row[2],
                    "execution_id": row[3],
                    "context": row[4],
                    "ttl_seconds": row[5],
                    "acquired_at": row[6],
                    "renewed_at": row[7],
                    "expires_at": row[8],
                    "status": row[9],
                }
                for row in rows
            ]
        finally:
            connection.close()

    def _enforce_compatibility(self, connection: sqlite3.Connection, request: ResourceLock) -> None:
        if request.lock_type == LockType.READ_SHARED:
            return
        cursor = connection.execute(
            "SELECT owner, lock_type FROM resource_locks WHERE lock_key = ? AND status = ?",
            (request.lock_key, LockStatus.ACTIVE.value),
        )
        for owner, existing_type in cursor.fetchall():
            if owner == request.owner:
                continue
            pair = (request.lock_type.value, existing_type)
            if pair in _INCOMPATIBLE_LOCK_PAIRS:
                raise LockError(
                    f"lock conflict on {request.lock_key}: {existing_type} held by {owner}"
                )

    def _reap_expired(self, connection: sqlite3.Connection) -> None:
        now = self._now()
        connection.execute(
            "UPDATE resource_locks SET status = ? WHERE status = ? AND expires_at <= ?",
            (LockStatus.EXPIRED.value, LockStatus.ACTIVE.value, now),
        )

    def _expires_at(self, now: str, ttl_seconds: int) -> str | None:
        if ttl_seconds <= 0:
            return None
        dt = datetime.fromisoformat(now)
        return (dt + timedelta(seconds=ttl_seconds)).isoformat()

    def _row_to_lock(self, lock_key: str, owner: str, row: tuple[Any, ...]) -> ResourceLock:
        (
            lock_type,
            execution_id,
            context,
            ttl_seconds,
            acquired_at,
            renewed_at,
            expires_at,
            status,
            metadata_json,
        ) = row
        return ResourceLock(
            lock_key=lock_key,
            lock_type=LockType(lock_type),
            owner=owner,
            execution_id=execution_id,
            context=context,
            ttl_seconds=ttl_seconds,
            acquired_at=acquired_at,
            renewed_at=renewed_at,
            expires_at=expires_at,
            status=LockStatus(status),
            metadata=json.loads(metadata_json or "{}"),
        )
