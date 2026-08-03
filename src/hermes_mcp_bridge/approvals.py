"""Persistent approval registry: lifecycle, expiry, stale detection, single-use consumption."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from .config import get_settings
from .protocol import ApprovalRecord, ApprovalStatus

_APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,159}$")
_APPROVAL_ID_MAX_LENGTH = 160


class RegistryError(Exception):
    """Base registry error with sanitized message."""


class ApprovalNotFound(RegistryError):
    """Approval record not found."""


class ApprovalStaleError(RegistryError):
    """Approval fingerprint/SHA differs from approved resource."""


class ApprovalExpiredError(RegistryError):
    """Approval has expired."""


class ApprovalStatusError(RegistryError):
    """Approval is in a non-approval status."""


class ApprovalConsumedError(RegistryError):
    """Approval was already consumed."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _validate_approval_id(approval_id: str) -> None:
    if not (1 <= len(approval_id) <= _APPROVAL_ID_MAX_LENGTH):
        raise RegistryError("approval_id must be 1..160 characters long")
    if not _APPROVAL_ID_RE.match(approval_id):
        raise RegistryError("approval_id contains invalid characters")


def _open_connection(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.isolation_level = None
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _resource_fingerprint(
    resource: str | None,
    metadata_sanitized: dict[str, Any] | None,
) -> str | None:
    payload = {"resource": resource, "metadata": metadata_sanitized or {}}
    normalized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ApprovalRegistry:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()

    def initialize(self) -> None:
        with self._lock:
            connection = _open_connection(self._db_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
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
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_approvals_decision_created_at
                    ON approvals (decision, created_at)
                    """
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def health(self) -> dict[str, object]:
        try:
            connection = _open_connection(self._db_path)
            try:
                cursor = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='approvals'"
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
                return {"status": "up", "table_exists": table_exists, "pragmas": pragmas}
            finally:
                connection.close()
        except Exception as error:
            return {"status": "down", "error": str(error)}

    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        _validate_approval_id(record.approval_id)
        now = _utcnow().isoformat()
        if record.created_at is None:
            record.created_at = now
        connection = _open_connection(self._db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR ABORT INTO approvals (
                    approval_id, action, resource, resource_fingerprint,
                    principal, delegation_chain_sanitized, decision,
                    expires_at, created_at, decided_at, consumed_at,
                    metadata_sanitized, approval_identity_assurance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.approval_id,
                    record.action,
                    record.resource,
                    record.resource_fingerprint,
                    record.principal,
                    json.dumps(record.delegation_chain_sanitized, ensure_ascii=False),
                    record.decision.value,
                    record.expires_at,
                    record.created_at,
                    record.decided_at,
                    record.consumed_at,
                    json.dumps(record.metadata_sanitized, ensure_ascii=False, sort_keys=True),
                    record.approval_identity_assurance,
                ),
            )
            connection.execute("COMMIT")
        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            raise
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return self.get(record.approval_id)

    def get(self, approval_id: str) -> ApprovalRecord:
        _validate_approval_id(approval_id)
        connection = _open_connection(self._db_path)
        try:
            cursor = connection.execute(
                """
                SELECT approval_id, action, resource, resource_fingerprint,
                       principal, delegation_chain_sanitized, decision,
                       expires_at, created_at, decided_at, consumed_at,
                       metadata_sanitized, approval_identity_assurance
                FROM approvals WHERE approval_id = ?
                """,
                (approval_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ApprovalNotFound(f"approval not found: {approval_id}")
            return self._from_row(row)
        finally:
            connection.close()

    def respond(
        self,
        approval_id: str,
        decision: ApprovalStatus,
        *,
        principal: str | None = None,
    ) -> ApprovalRecord:
        _validate_approval_id(approval_id)
        now = _utcnow().isoformat()
        connection = _open_connection(self._db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "SELECT decision, expires_at, resource_fingerprint"
                " FROM approvals WHERE approval_id = ?",
                (approval_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ApprovalNotFound(f"approval not found: {approval_id}")
            current_decision = ApprovalStatus(row[0])
            if current_decision != ApprovalStatus.REQUESTED:
                raise ApprovalStatusError(
                    f"approval not in requested state: {current_decision.value}"
                )
            expires_at = row[1]
            if expires_at is not None and datetime.fromisoformat(expires_at) <= _utcnow():
                raise ApprovalExpiredError("approval expired")
            connection.execute(
                """
                UPDATE approvals
                SET decision = ?, decided_at = ?, principal = COALESCE(?, principal)
                WHERE approval_id = ?
                """,
                (decision.value, now, principal, approval_id),
            )
            connection.execute("COMMIT")
        except Exception:
            with suppress(Exception):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return self.get(approval_id)

    def consume(self, approval_id: str, resource_fingerprint: str | None) -> ApprovalRecord:
        _validate_approval_id(approval_id)
        now = _utcnow().isoformat()
        connection = _open_connection(self._db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "SELECT decision, resource_fingerprint, consumed_at, created_at"
                " FROM approvals WHERE approval_id = ?",
                (approval_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ApprovalNotFound(f"approval not found: {approval_id}")
            decision = ApprovalStatus(row[0])
            if decision == ApprovalStatus.CONSUMED:
                raise ApprovalConsumedError("approval already consumed")
            if decision != ApprovalStatus.APPROVED:
                raise ApprovalStatusError(f"approval not approved: {decision.value}")
            if row[2] is not None:
                raise ApprovalConsumedError("approval already consumed")
            if row[3] is not None and row[3] > now:
                raise ApprovalExpiredError("approval created in the future")
            if (
                resource_fingerprint is not None
                and row[1] is not None
                and resource_fingerprint != row[1]
            ):
                raise ApprovalStaleError("resource fingerprint differs from approved fingerprint")
            if row[1] is not None and resource_fingerprint is None:
                raise ApprovalStaleError("approval requires resource fingerprint for consumption")
            connection.execute(
                "UPDATE approvals SET consumed_at = ?, decision = ? WHERE approval_id = ?",
                (now, ApprovalStatus.CONSUMED.value, approval_id),
            )
            connection.execute("COMMIT")
        except Exception:
            with suppress(Exception):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return self.get(approval_id)

    def mark_stale(self, approval_id: str) -> ApprovalRecord:
        _validate_approval_id(approval_id)
        connection = _open_connection(self._db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "SELECT decision FROM approvals WHERE approval_id = ?",
                (approval_id,),
            )
            row = cursor.fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise ApprovalNotFound(f"approval not found: {approval_id}")
            if ApprovalStatus(row[0]) in {
                ApprovalStatus.STALE,
                ApprovalStatus.CONSUMED,
                ApprovalStatus.REJECTED,
            }:
                connection.execute("ROLLBACK")
                return self.get(approval_id)
            now = _utcnow().isoformat()
            connection.execute(
                "UPDATE approvals SET decision = ?, decided_at = ? WHERE approval_id = ?",
                (ApprovalStatus.STALE.value, now, approval_id),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return self.get(approval_id)

    def expire(self, approval_id: str) -> ApprovalRecord:
        _validate_approval_id(approval_id)
        connection = _open_connection(self._db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "SELECT decision FROM approvals WHERE approval_id = ?",
                (approval_id,),
            )
            row = cursor.fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise ApprovalNotFound(f"approval not found: {approval_id}")
            if ApprovalStatus(row[0]) != ApprovalStatus.REQUESTED:
                connection.execute("ROLLBACK")
                return self.get(approval_id)
            now = _utcnow().isoformat()
            connection.execute(
                "UPDATE approvals SET decision = ?, decided_at = ? WHERE approval_id = ?",
                (ApprovalStatus.EXPIRED.value, now, approval_id),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return self.get(approval_id)

    def list_recent(self, *, limit: int = 50) -> list[ApprovalRecord]:
        limit = max(1, min(100, limit))
        connection = _open_connection(self._db_path)
        try:
            cursor = connection.execute(
                """
                SELECT approval_id, action, resource, resource_fingerprint,
                       principal, delegation_chain_sanitized, decision,
                       expires_at, created_at, decided_at, consumed_at,
                       metadata_sanitized, approval_identity_assurance
                FROM approvals
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [self._from_row(row) for row in cursor.fetchall()]
        finally:
            connection.close()

    def _from_row(self, row: tuple[Any, ...]) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=str(row[0]),
            action=str(row[1]),
            resource=row[2],
            resource_fingerprint=row[3],
            principal=row[4],
            delegation_chain_sanitized=json.loads(row[5] or "[]"),
            decision=ApprovalStatus(str(row[6])),
            expires_at=row[7],
            created_at=str(row[8]),
            decided_at=row[9],
            consumed_at=row[10],
            metadata_sanitized=json.loads(row[11] or "{}"),
            approval_identity_assurance=str(row[12]),
        )


_approval_registry: ApprovalRegistry | None = None
_registry_lock = threading.Lock()


def get_approval_registry() -> ApprovalRegistry:
    global _approval_registry
    if _approval_registry is None:
        with _registry_lock:
            if _approval_registry is None:
                _approval_registry = ApprovalRegistry(get_settings().bridge_state_db_path)
    return _approval_registry


def approval_record_from_evaluation(
    evaluation_id: str,
    evaluation: Any,
    result: Any,
    *,
    principal: str | None = None,
    metadata_sanitized: dict[str, Any] | None = None,
) -> ApprovalRecord:
    resource = None
    resource_fp = None
    if hasattr(evaluation, "resource") and evaluation.resource:
        resource = str(evaluation.resource)
        resource_fp = _resource_fingerprint(resource, metadata_sanitized)
    return ApprovalRecord(
        approval_id=evaluation_id,
        action=str(evaluation.action),
        resource=resource,
        resource_fingerprint=resource_fp,
        principal=principal,
        delegation_chain_sanitized=list(getattr(evaluation, "delegation_chain", []) or []),
        created_at=_utcnow().isoformat(),
        metadata_sanitized=metadata_sanitized or {},
    )
