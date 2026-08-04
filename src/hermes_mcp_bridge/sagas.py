"""Saga registry."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from .models import Saga, SagaStatus, SagaStep


class SagaRegistry:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._global_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        from .migrations import apply_migrations
        apply_migrations(self._db_path)
        self._initialized = True

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def create(self, saga: Saga) -> Saga:
        saga.created_at = saga.created_at or self._now()
        saga.updated_at = saga.created_at
        with self._global_lock:
            connection = sqlite3.connect(self._db_path, check_same_thread=False)
            connection.isolation_level = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO sagas (
                        saga_id, execution_id, current_step, status,
                        steps_json, state_json, trace_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        saga.saga_id,
                        saga.execution_id,
                        saga.current_step,
                        saga.status.value,
                        json.dumps(
                            [step.model_dump(mode="json") for step in saga.steps],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        json.dumps(saga.state, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(
                            saga.trace.model_dump(mode="json") if saga.trace else {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        saga.created_at,
                        saga.updated_at,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        return saga

    def get(self, saga_id: str) -> Saga | None:
        import sqlite3
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.isolation_level = None
        try:
            cursor = connection.execute(
                "SELECT execution_id, current_step, status, "
                "steps_json, state_json, trace_json, created_at, updated_at "
                "FROM sagas WHERE saga_id = ?",
                (saga_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_saga(saga_id, row)
        finally:
            connection.close()

    def update_status(
        self, saga_id: str, status: SagaStatus, current_step: str | None = None
    ) -> Saga | None:
        now = self._now()
        with self._global_lock:
            import sqlite3
            connection = sqlite3.connect(self._db_path, check_same_thread=False)
            connection.isolation_level = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "SELECT steps_json, state_json, trace_json FROM sagas WHERE saga_id = ?",
                    (saga_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return None
                if current_step is not None:
                    connection.execute(
                        "UPDATE sagas SET status = ?, current_step = ?, updated_at = ? "
                        "WHERE saga_id = ?",
                        (status.value, current_step, now, saga_id),
                    )
                else:
                    connection.execute(
                        "UPDATE sagas SET status = ?, updated_at = ? WHERE saga_id = ?",
                        (status.value, now, saga_id),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        return self.get(saga_id)

    def _row_to_saga(self, saga_id: str, row: tuple[Any, ...]) -> Saga:
        (
            execution_id,
            current_step,
            status,
            steps_json,
            state_json,
            trace_json,
            created_at,
            updated_at,
        ) = row
        steps = [SagaStep(**step) for step in json.loads(steps_json or "[]")]
        trace_data = json.loads(trace_json or "{}")
        return Saga(
            saga_id=saga_id,
            execution_id=execution_id,
            current_step=current_step,
            status=SagaStatus(status),
            steps=steps,
            state=json.loads(state_json or "{}"),
            trace=trace_data,
            created_at=created_at,
            updated_at=updated_at,
        )
