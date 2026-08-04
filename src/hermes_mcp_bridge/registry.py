from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import weakref
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from hermes_mcp_bridge.config import get_settings
from hermes_mcp_bridge.observability.instrumentation import record_sqlite_operation

CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,159}$")
_CLIENT_REQUEST_ID_MAX_LENGTH = 160


class RegistryError(Exception):
    """Base registry error with sanitized message."""


class ClientRequestIdError(RegistryError):
    """Invalid client request id."""


class FingerprintConflictError(RegistryError):
    """Conflicting fingerprint for client_request_id."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_prompt(prompt: str) -> str:
    return "\n".join(prompt.strip().splitlines())


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_json_value(value[key]) for key in sorted(value.keys())}
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    return value


def _normalize_json_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _normalize_json_value(payload)


def _compute_fingerprint(
    *,
    prompt: str,
    session_id: str | None,
    agent: str | None,
    subagents: list[str] | None,
    orchestration: str | None,
) -> str:
    payload = _normalize_json_payload(
        {
            "prompt": _normalize_prompt(prompt),
            "session_id": session_id,
            "agent": agent,
            "subagents": sorted(subagents or []),
            "orchestration": orchestration,
        }
    )
    normalized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_client_request_id(client_request_id: str) -> None:
    if not (1 <= len(client_request_id) <= _CLIENT_REQUEST_ID_MAX_LENGTH):
        raise ClientRequestIdError("client_request_id must be 1..160 characters long")
    if not CLIENT_REQUEST_ID_RE.match(client_request_id):
        raise ClientRequestIdError("client_request_id contains invalid characters")


def _open_connection(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.isolation_level = None
    return connection


class RunRegistry:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._key_locks: weakref.WeakValueDictionary[str, threading.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._global_lock = threading.Lock()
        self._initialized = False

    def _key_lock(self, client_request_id: str) -> threading.Lock:
        with self._global_lock:
            lock = self._key_locks.get(client_request_id)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[client_request_id] = lock
        return lock

    def initialize(self) -> None:
        with self._global_lock:
            if self._initialized:
                return
            from .migrations import apply_migrations

            apply_migrations(self._db_path)
            self._initialized = True

    def health(self) -> dict[str, object]:
        try:
            connection = _open_connection(self._db_path)
            try:
                cursor = connection.execute(
                    "SELECT name FROM sqlite_master"
                    " WHERE type='table' AND name='run_mappings'"
                )
                table_exists = cursor.fetchone() is not None
                pragmas: dict[str, str] = {}
                if table_exists:
                    pragmas["journal_mode"] = (
                        connection.execute("PRAGMA journal_mode").fetchone()[0]
                    )
                    pragmas["synchronous"] = (
                        connection.execute("PRAGMA synchronous").fetchone()[0]
                    )
                    pragmas["busy_timeout"] = (
                        connection.execute("PRAGMA busy_timeout").fetchone()[0]
                    )
                return {"status": "up", "table_exists": table_exists, "pragmas": pragmas}
            finally:
                connection.close()
        except Exception as error:
            record_sqlite_operation(kind="state", outcome="error", exc=error)
            return {"status": "down", "error": str(error)}

    def get(self, client_request_id: str) -> dict[str, object] | None:
        _validate_client_request_id(client_request_id)
        connection = _open_connection(self._db_path)
        try:
            cursor = connection.execute(
                "SELECT client_request_id, fingerprint, execution_id, session_id,"
                " last_status, created_at, updated_at"
                " FROM run_mappings WHERE client_request_id = ?",
                (client_request_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "client_request_id": row[0],
                "fingerprint": row[1],
                "execution_id": row[2],
                "session_id": row[3],
                "last_status": row[4],
                "created_at": row[5],
                "updated_at": row[6],
            }
        finally:
            connection.close()

    def record(
        self,
        *,
        client_request_id: str,
        fingerprint: str,
        execution_id: str,
        session_id: str | None = None,
        last_status: str = "queued",
    ) -> dict[str, object]:
        _validate_client_request_id(client_request_id)
        now = _utcnow().isoformat()
        with self._key_lock(client_request_id):
            connection = None
            try:
                connection = _open_connection(self._db_path)
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "SELECT fingerprint FROM run_mappings WHERE client_request_id = ?",
                    (client_request_id,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    stored_fingerprint = existing[0]
                    if stored_fingerprint != fingerprint:
                        raise FingerprintConflictError(
                            "existing mapping has a different fingerprint"
                        )
                    connection.execute(
                        "UPDATE run_mappings SET updated_at = ?"
                        " WHERE client_request_id = ?",
                        (now, client_request_id),
                    )
                else:
                    connection.execute(
                        "INSERT INTO run_mappings"
                        " (client_request_id, fingerprint, execution_id,"
                        " session_id, last_status, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            client_request_id,
                            fingerprint,
                            execution_id,
                            session_id,
                            last_status,
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception as exc:
                record_sqlite_operation(kind="state", outcome="error", exc=exc)
                if connection is not None:
                    with suppress(Exception):
                        connection.execute("ROLLBACK")
                        connection.close()
                raise
            connection.close()
        return self.get(client_request_id)

    def update_status(
        self,
        *,
        client_request_id: str,
        last_status: str,
        execution_id: str | None = None,
    ) -> dict[str, object] | None:
        _validate_client_request_id(client_request_id)
        now = _utcnow().isoformat()
        with self._key_lock(client_request_id):
            connection = None
            try:
                connection = _open_connection(self._db_path)
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "SELECT fingerprint FROM run_mappings WHERE client_request_id = ?",
                    (client_request_id,),
                )
                if cursor.fetchone() is None:
                    connection.execute("ROLLBACK")
                    connection.close()
                    return None
                if execution_id is None:
                    connection.execute(
                        "UPDATE run_mappings SET last_status = ?, updated_at = ?"
                        " WHERE client_request_id = ?",
                        (last_status, now, client_request_id),
                    )
                else:
                    connection.execute(
                        "UPDATE run_mappings SET last_status = ?, execution_id = ?,"
                        " updated_at = ? WHERE client_request_id = ?",
                        (last_status, execution_id, now, client_request_id),
                    )
                connection.execute("COMMIT")
                connection.close()
                return self.get(client_request_id)
            except Exception as exc:
                record_sqlite_operation(kind="state", outcome="error", exc=exc)
                if connection is not None:
                    with suppress(Exception):
                        connection.execute("ROLLBACK")
                        connection.close()
                raise
    def list_recent(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict[str, object]]:
        limit = max(1, min(100, limit))
        connection = _open_connection(self._db_path)
        try:
            query = "SELECT client_request_id, execution_id, session_id,"
            query += " last_status, created_at, updated_at FROM run_mappings"
            params: list[object] = []
            if status is not None:
                query += " WHERE last_status = ?"
                params.append(status)
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            cursor = connection.execute(query, params)
            return [
                {
                    "client_request_id": row[0],
                    "execution_id": row[1],
                    "session_id": row[2],
                    "last_status": row[3],
                    "created_at": row[4],
                    "updated_at": row[5],
                }
                for row in cursor.fetchall()
            ]
        finally:
            connection.close()


_registry: RunRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> RunRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = RunRegistry(get_settings().bridge_state_db_path)
    return _registry


def compute_fingerprint(
    *,
    prompt: str,
    session_id: str | None = None,
    agent: str | None = None,
    subagents: list[str] | None = None,
    orchestration: str | None = None,
) -> str:
    return _compute_fingerprint(
        prompt=prompt,
        session_id=session_id,
        agent=agent,
        subagents=subagents,
        orchestration=orchestration,
    )
