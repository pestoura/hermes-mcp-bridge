"""Quota registry and gating."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from .models import QuotaDecision, QuotaProfile


class QuotaError(RuntimeError):
    """Raised when a quota gate rejects or throttles an action."""


class QuotaRegistry:
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

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.isolation_level = None
        return connection

    def ensure_default_profile(self) -> QuotaProfile:
        profile = QuotaProfile(profile_id="default")
        with self._global_lock:
            connection = self._open()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO quota_profiles (
                        profile_id, max_parallel_runs,
                        max_parallel_mutations_per_resource,
                        max_runtime_seconds, max_tool_calls, max_tokens,
                        priority, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile.profile_id,
                        profile.max_parallel_runs,
                        profile.max_parallel_mutations_per_resource,
                        profile.max_runtime_seconds,
                        profile.max_tool_calls,
                        profile.max_tokens,
                        profile.priority,
                        json.dumps({}, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        return profile

    def evaluate(
        self,
        *,
        principal: str | None = None,
        resource: str | None = None,
        mutation: bool = False,
    ) -> dict[str, Any]:
        profile = self.ensure_default_profile()
        decision = QuotaDecision.ALLOW
        reason = "within_quota"
        if mutation and profile.max_parallel_mutations_per_resource <= 0:
            decision = QuotaDecision.REJECT
            reason = "mutations_disabled_for_profile"
        return {
            "decision": decision.value,
            "reason": reason,
            "profile_id": profile.profile_id,
            "principal": principal,
            "resource": resource,
        }

    def status(self) -> dict[str, Any]:
        connection = self._open()
        try:
            cursor = connection.execute(
                "SELECT profile_id, max_parallel_runs, "
                "max_parallel_mutations_per_resource, max_runtime_seconds, "
                "max_tool_calls, max_tokens, priority FROM quota_profiles"
            )
            rows = cursor.fetchall()
            return {
                "profiles": [
                    {
                        "profile_id": row[0],
                        "max_parallel_runs": row[1],
                        "max_parallel_mutations_per_resource": row[2],
                        "max_runtime_seconds": row[3],
                        "max_tool_calls": row[4],
                        "max_tokens": row[5],
                        "priority": row[6],
                    }
                    for row in rows
                ]
            }
        finally:
            connection.close()


_quota_registry: QuotaRegistry | None = None
_quota_lock = threading.Lock()


def get_quota_registry() -> QuotaRegistry:
    global _quota_registry
    if _quota_registry is None:
        with _quota_lock:
            if _quota_registry is None:
                from .config import get_settings
                _quota_registry = QuotaRegistry(get_settings().bridge_state_db_path)
    return _quota_registry
