"""SQLite-backed checkpoint and continuation registry."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from .models import Checkpoint, Continuation, TraceContext

DDL = """
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    state_ref TEXT,
    evidence_refs_json TEXT,
    trace_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS continuations (
    continuation_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    checkpoint_id TEXT,
    continuation_of TEXT,
    mode TEXT NOT NULL,
    resume_supported INTEGER NOT NULL,
    trace_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_execution
    ON checkpoints (execution_id);
CREATE INDEX IF NOT EXISTS idx_continuations_execution
    ON continuations (execution_id);
"""


class CheckpointRegistry:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._global_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        with self._global_lock:
            if self._initialized:
                return
            from .migrations import apply_migrations

            apply_migrations(self._db_path)
            self._initialized = True

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _row_to_checkpoint(self, row: tuple[str, str, str, int, str, str, str, str]) -> Checkpoint:
        (
            checkpoint_id,
            execution_id,
            phase,
            step_index,
            state_ref,
            evidence_refs_json,
            trace_json,
            created_at,
        ) = row
        return Checkpoint(
            checkpoint_id=checkpoint_id,
            execution_id=execution_id,
            phase=phase,
            step_index=step_index,
            state_ref=state_ref,
            evidence_refs=(
                json.loads(evidence_refs_json)
                if evidence_refs_json
                else []
            ),
            trace=(
                TraceContext.model_validate_json(trace_json)
                if trace_json
                else None
            ),
            created_at=created_at,
        )

    def create(self, checkpoint: Checkpoint) -> Checkpoint:
        with self._global_lock, sqlite3.connect(self._db_path, check_same_thread=False) as cx:
            cx.execute(
                """
                INSERT INTO checkpoints
                    (checkpoint_id, execution_id, phase, step_index,
                     state_ref, evidence_refs_json, trace_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.execution_id,
                    checkpoint.phase,
                    checkpoint.step_index,
                    checkpoint.state_ref,
                    json.dumps(
                        checkpoint.evidence_refs,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        checkpoint.trace.model_dump(mode="json")
                        if checkpoint.trace
                        else {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    self._now(),
                ),
            )
        return checkpoint

    def status(
        self,
        execution_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT checkpoint_id, execution_id, phase, step_index, state_ref, "
            "evidence_refs_json, trace_json, created_at FROM checkpoints"
        )
        params: list[Any] = []
        if checkpoint_id:
            query += " WHERE checkpoint_id = ?"
            params.append(checkpoint_id)
        elif execution_id:
            query += " WHERE execution_id = ?"
            params.append(execution_id)
        with sqlite3.connect(self._db_path, check_same_thread=False) as cx:
            cx.row_factory = sqlite3.Row
            rows = cx.execute(query, params).fetchall()
        return [
            {
                "checkpoint_id": r["checkpoint_id"],
                "execution_id": r["execution_id"],
                "phase": r["phase"],
                "step_index": r["step_index"],
                "state_ref": r["state_ref"],
                "evidence_refs": json.loads(r["evidence_refs_json"] or "[]"),
                "trace": (
                    json.loads(r["trace_json"])
                    if r["trace_json"]
                    else None
                ),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def add_continuation(self, continuation: Continuation) -> Continuation:
        with self._global_lock, sqlite3.connect(self._db_path, check_same_thread=False) as cx:
            cx.execute(
                """
                INSERT INTO continuations
                    (continuation_id, execution_id, checkpoint_id,
                     continuation_of, mode, resume_supported,
                     trace_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    continuation.continuation_id,
                    continuation.execution_id,
                    continuation.checkpoint_id,
                    continuation.continuation_of,
                    continuation.mode,
                    1 if continuation.resume_supported else 0,
                    json.dumps(
                        continuation.trace.model_dump(mode="json")
                        if continuation.trace
                        else {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    self._now(),
                ),
            )
        return continuation

    def list_continuations(self, execution_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db_path, check_same_thread=False) as cx:
            cx.row_factory = sqlite3.Row
            rows = cx.execute(
                "SELECT continuation_id, execution_id, checkpoint_id, "
                "continuation_of, mode, resume_supported, trace_json, "
                "created_at FROM continuations WHERE execution_id = ?",
                (execution_id,),
            ).fetchall()
        return [
            {
                "continuation_id": r["continuation_id"],
                "execution_id": r["execution_id"],
                "checkpoint_id": r["checkpoint_id"],
                "continuation_of": r["continuation_of"],
                "mode": r["mode"],
                "resume_supported": bool(r["resume_supported"]),
                "trace": (
                    json.loads(r["trace_json"]) if r["trace_json"] else None
                ),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
