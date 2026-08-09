"""Phase 5 durable checkpoint/lease store (OD-003 closed; ADR-0024).

> **V2 · PHASE 5 · runtime**

Decision: **SQLite in WAL mode, single local file, stdlib ``sqlite3`` only**.

Rationale over the alternatives:

* no new dependency, no daemon, no network listener and no credential of its
  own — the smallest attack surface that still gives durability;
* ``BEGIN IMMEDIATE`` plus a monotonic ``fence_token`` column gives real
  compare-and-set, which is what lease fencing needs;
* the file is local and can be backed up/inspected with the existing evidence
  tooling; a dedicated queue or cloud store would add an operational and
  authorization boundary Phase 5 does not need.

Every write is (a) fenced, (b) integrity-digested, and (c) durable before the
caller proceeds. Reads verify ``record_digest`` and refuse tampered records.
The store never holds credential material: node states carry shaped results and
digests only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .canonical import canonical_json_bytes, sha256_hex
from .dag_contract import NodeStatus, PlanReason, PlanStatus, PlanValidationError

#: State schema version. A resume against an unsupported version fails closed.
CHECKPOINT_SCHEMA_VERSION = "dagstate/1"

_SECRET_HINTS = (
    "access_token",
    "refresh_token",
    "api_token",
    "auth_token",
    "id_token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "private_key",
    "client_secret",
    "bearer",
)


class StoreError(PlanValidationError):
    """Store-layer failure. Always fail-closed for the caller."""


def assert_no_secret_material(payload: Any, *, where: str) -> None:
    """Reject any key that looks like credential material. Fail-closed."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _SECRET_HINTS):
                raise StoreError(PlanReason.CHECKPOINT_TAMPERED, f"{where}: secret-like field")
            assert_no_secret_material(value, where=where)
    elif isinstance(payload, list | tuple):
        for item in payload:
            assert_no_secret_material(item, where=where)


@dataclass(frozen=True, slots=True)
class Lease:
    owner_id: str
    fence_token: int
    expires_at_ms: int
    heartbeat_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class NodeState:
    node_id: str
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = 0
    idempotency_key: str | None = None
    effect_ref: str | None = None
    result: Any = None
    reason_code: str | None = None
    operation_digest: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status.value,
            "attempt": self.attempt,
            "idempotency_key": self.idempotency_key,
            "effect_ref": self.effect_ref,
            "result": self.result,
            "reason_code": self.reason_code,
            "operation_digest": self.operation_digest,
        }

    @staticmethod
    def from_dict(payload: Mapping[str, Any]) -> NodeState:
        return NodeState(
            node_id=str(payload["node_id"]),
            status=NodeStatus(str(payload["status"])),
            attempt=int(payload.get("attempt", 0)),
            idempotency_key=payload.get("idempotency_key"),
            effect_ref=payload.get("effect_ref"),
            result=payload.get("result"),
            reason_code=payload.get("reason_code"),
            operation_digest=payload.get("operation_digest"),
        )


@dataclass(frozen=True, slots=True)
class Checkpoint:
    execution_id: str
    plan_digest: str
    principal_ref: str
    projection_digest: str
    policy_digest: str
    lease: Lease
    schema_version: str = CHECKPOINT_SCHEMA_VERSION
    approval_ref: str | None = None
    status: PlanStatus | None = None
    node_states: Mapping[str, NodeState] = field(default_factory=dict)
    budget_consumed: Mapping[str, int] = field(default_factory=dict)
    replay: bool = False

    def body(self) -> dict[str, Any]:
        """Canonical, digestible body. Excludes the integrity digest itself."""
        return {
            "execution_id": self.execution_id,
            "plan_digest": self.plan_digest,
            "principal_ref": self.principal_ref,
            "projection_digest": self.projection_digest,
            "policy_digest": self.policy_digest,
            "schema_version": self.schema_version,
            "approval_ref": self.approval_ref,
            "status": self.status.value if self.status else None,
            "replay": self.replay,
            "budget_consumed": dict(sorted(self.budget_consumed.items())),
            "lease": {
                "owner_id": self.lease.owner_id,
                "fence_token": self.lease.fence_token,
                "expires_at_ms": self.lease.expires_at_ms,
                "heartbeat_at_ms": self.lease.heartbeat_at_ms,
            },
            "node_states": [state.as_dict() for _, state in sorted(self.node_states.items())],
        }

    def record_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self.body()))

    def with_node(self, state: NodeState) -> Checkpoint:
        states = dict(self.node_states)
        states[state.node_id] = state
        return replace(self, node_states=states)


def checkpoint_from_body(payload: Mapping[str, Any]) -> Checkpoint:
    lease = payload["lease"]
    return Checkpoint(
        execution_id=str(payload["execution_id"]),
        plan_digest=str(payload["plan_digest"]),
        principal_ref=str(payload["principal_ref"]),
        projection_digest=str(payload["projection_digest"]),
        policy_digest=str(payload["policy_digest"]),
        schema_version=str(payload["schema_version"]),
        approval_ref=payload.get("approval_ref"),
        status=PlanStatus(payload["status"]) if payload.get("status") else None,
        replay=bool(payload.get("replay", False)),
        budget_consumed=dict(payload.get("budget_consumed", {})),
        lease=Lease(
            owner_id=str(lease["owner_id"]),
            fence_token=int(lease["fence_token"]),
            expires_at_ms=int(lease["expires_at_ms"]),
            heartbeat_at_ms=int(lease.get("heartbeat_at_ms", 0)),
        ),
        node_states={
            str(entry["node_id"]): NodeState.from_dict(entry)
            for entry in payload.get("node_states", [])
        },
    )


@runtime_checkable
class CheckpointStore(Protocol):
    """Durable store contract. Every write is fenced and integrity-digested."""

    durable: bool

    def create(self, checkpoint: Checkpoint) -> None: ...

    def load(self, execution_id: str) -> Checkpoint: ...

    def save(self, checkpoint: Checkpoint, *, fence_token: int) -> None: ...

    def acquire_lease(self, execution_id: str, owner_id: str, expires_at_ms: int) -> Lease: ...

    def consume_approval(self, approval_id: str, nonce: str, execution_id: str) -> bool: ...


class SqliteCheckpointStore:
    """WAL-mode SQLite implementation of :class:`CheckpointStore`."""

    durable = True

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS dag_checkpoint ("
            " execution_id TEXT PRIMARY KEY,"
            " plan_digest TEXT NOT NULL,"
            " fence_token INTEGER NOT NULL,"
            " record_digest TEXT NOT NULL,"
            " body TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS dag_approval ("
            " approval_id TEXT NOT NULL,"
            " nonce TEXT NOT NULL,"
            " execution_id TEXT NOT NULL,"
            " PRIMARY KEY (approval_id, nonce))"
        )

    def close(self) -> None:
        self._conn.close()

    def create(self, checkpoint: Checkpoint) -> None:
        assert_no_secret_material(checkpoint.body(), where="checkpoint")
        body = canonical_json_bytes(checkpoint.body()).decode("utf-8")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO dag_checkpoint"
                    " (execution_id, plan_digest, fence_token, record_digest, body)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        checkpoint.execution_id,
                        checkpoint.plan_digest,
                        checkpoint.lease.fence_token,
                        checkpoint.record_digest(),
                        body,
                    ),
                )
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                raise StoreError(PlanReason.PLAN_SCHEMA_INVALID, "duplicate execution") from exc

    def load(self, execution_id: str) -> Checkpoint:
        row = self._conn.execute(
            "SELECT record_digest, body FROM dag_checkpoint WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if row is None:
            raise StoreError(PlanReason.CHECKPOINT_TAMPERED, "unknown execution")
        stored_digest, body = row
        payload = json.loads(body)
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise StoreError(PlanReason.CHECKPOINT_SCHEMA_UNSUPPORTED, "state schema")
        checkpoint = checkpoint_from_body(payload)
        if checkpoint.record_digest() != stored_digest:
            raise StoreError(PlanReason.CHECKPOINT_TAMPERED, "record digest")
        return checkpoint

    def save(self, checkpoint: Checkpoint, *, fence_token: int) -> None:
        assert_no_secret_material(checkpoint.body(), where="checkpoint")
        body = canonical_json_bytes(checkpoint.body()).decode("utf-8")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT fence_token FROM dag_checkpoint WHERE execution_id = ?",
                (checkpoint.execution_id,),
            ).fetchone()
            if row is None:
                self._conn.execute("ROLLBACK")
                raise StoreError(PlanReason.CHECKPOINT_TAMPERED, "unknown execution")
            if fence_token < int(row[0]):
                self._conn.execute("ROLLBACK")
                raise StoreError(PlanReason.LEASE_FENCE_STALE, "stale fence token")
            self._conn.execute(
                "UPDATE dag_checkpoint SET fence_token = ?, record_digest = ?, body = ?"
                " WHERE execution_id = ? AND fence_token <= ?",
                (
                    fence_token,
                    checkpoint.record_digest(),
                    body,
                    checkpoint.execution_id,
                    fence_token,
                ),
            )
            self._conn.execute("COMMIT")

    def acquire_lease(self, execution_id: str, owner_id: str, expires_at_ms: int) -> Lease:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT fence_token, body FROM dag_checkpoint WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                self._conn.execute("ROLLBACK")
                raise StoreError(PlanReason.CHECKPOINT_TAMPERED, "unknown execution")
            token = int(row[0]) + 1
            payload = json.loads(row[1])
            payload["lease"] = {
                "owner_id": owner_id,
                "fence_token": token,
                "expires_at_ms": expires_at_ms,
                "heartbeat_at_ms": payload["lease"].get("heartbeat_at_ms", 0),
            }
            checkpoint = checkpoint_from_body(payload)
            self._conn.execute(
                "UPDATE dag_checkpoint SET fence_token = ?, record_digest = ?, body = ?"
                " WHERE execution_id = ?",
                (
                    token,
                    checkpoint.record_digest(),
                    canonical_json_bytes(checkpoint.body()).decode("utf-8"),
                    execution_id,
                ),
            )
            self._conn.execute("COMMIT")
            return checkpoint.lease

    def consume_approval(self, approval_id: str, nonce: str, execution_id: str) -> bool:
        """Atomic single-use consumption. Exactly one concurrent caller wins."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO dag_approval (approval_id, nonce, execution_id) VALUES (?, ?, ?)",
                    (approval_id, nonce, execution_id),
                )
                self._conn.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                self._conn.execute("ROLLBACK")
                return False

    def approval_holder(self, approval_id: str, nonce: str) -> str | None:
        row = self._conn.execute(
            "SELECT execution_id FROM dag_approval WHERE approval_id = ? AND nonce = ?",
            (approval_id, nonce),
        ).fetchone()
        return None if row is None else str(row[0])


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "Checkpoint",
    "CheckpointStore",
    "Lease",
    "NodeState",
    "SqliteCheckpointStore",
    "StoreError",
    "assert_no_secret_material",
    "checkpoint_from_body",
]
