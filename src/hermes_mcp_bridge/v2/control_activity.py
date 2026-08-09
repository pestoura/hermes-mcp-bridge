"""Control-activity guard for the V2 Phase 2 OUTER final acceptance runner.

The final out-of-band acceptance may only measure the **real** Hermes state
database while no control run is alive. The previous guard probed tables
(``api_runs``, ``delegations``) that do not exist in the Hermes 0.20 state
schema: every lookup was skipped and the guard silently answered "quiet". That
is the exact failure mode a guard must never have.

This module re-implements the guard against the **actual** Hermes 0.20 schema
(``schema_version = 25``) and makes every unknown condition fatal:

* the tracked tables and the columns the guard depends on are introspected
  first; anything missing, renamed or of an unexpected shape yields
  :data:`STATUS_UNMEASURABLE` — never :data:`STATUS_QUIET`;
* only **bounded aggregates** are read: ``COUNT(*)`` over closed state
  vocabularies and one ``MAX()`` recency comparison. No row content, no
  identifier, no free-text column is ever read or returned;
* the report carries booleans and counters only, plus stable sanitized
  blocker codes.

Vocabulary
----------

``QUIET``        no live control indicator; measurement may proceed.
``ACTIVE``       at least one live indicator; the runner must abort.
``UNMEASURABLE`` the guard could not answer; the runner must abort.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

STATUS_QUIET: Final[str] = "QUIET"
STATUS_ACTIVE: Final[str] = "ACTIVE"
STATUS_UNMEASURABLE: Final[str] = "UNMEASURABLE"

CONTROL_ACTIVITY_SCHEMA: Final[str] = "hermes-v2-phase2-control-activity/1"

#: Hermes 0.20 tables the guard depends on, with the columns it reads.
#: Every entry must exist with every listed column, otherwise the guard is
#: UNMEASURABLE. Extra columns are tolerated (Hermes adds columns over time);
#: a *missing* column is not.
REQUIRED_TABLES: Final[dict[str, tuple[str, ...]]] = {
    "sessions": ("id", "ended_at", "last_activity_at"),
    "async_delegations": ("delegation_id", "state", "delivery_state"),
    "delivery_obligations": ("obligation_id", "state"),
    "compression_locks": ("session_id", "expires_at"),
}

#: Delegation states that mean work is still in flight.
ACTIVE_DELEGATION_STATES: Final[tuple[str, ...]] = (
    "pending",
    "dispatched",
    "running",
    "in_progress",
    "queued",
)

#: Delegation delivery states that mean a result is still being handed back.
ACTIVE_DELIVERY_STATES: Final[tuple[str, ...]] = (
    "pending",
    "claimed",
    "delivering",
    "retrying",
)

#: Obligation states that mean the gateway still owes a delivery.
ACTIVE_OBLIGATION_STATES: Final[tuple[str, ...]] = (
    "pending",
    "claimed",
    "delivering",
    "retrying",
)

#: A session touched within this window counts as a live control run. This is
#: the 0.20 replacement for the non-existent ``api_runs`` table: API/gateway
#: runs heartbeat ``sessions.last_activity_at``.
DEFAULT_RECENT_ACTIVITY_SECONDS: Final[int] = 300

_SQLITE_TIMEOUT_SECONDS: Final[float] = 5.0

BLOCKER_CODES: Final[frozenset[str]] = frozenset(
    {
        "CONTROL_DB_UNREADABLE",
        "CONTROL_DB_QUERY_FAILED",
        "CONTROL_SCHEMA_TABLE_MISSING",
        "CONTROL_SCHEMA_COLUMN_MISSING",
        "CONTROL_SCHEMA_UNEXPECTED_TYPE",
    }
)


@dataclass(frozen=True, slots=True)
class ControlActivityReport:
    """Sanitized guard verdict. Contains no path, identifier or row content."""

    status: str
    indicators: dict[str, int]
    blockers: tuple[str, ...]

    @property
    def quiet(self) -> bool:
        return self.status == STATUS_QUIET

    def as_canonical(self) -> dict[str, Any]:
        return {
            "schema": CONTROL_ACTIVITY_SCHEMA,
            "status": self.status,
            "active_delegations": int(self.indicators.get("active_delegations", 0)),
            "pending_deliveries": int(self.indicators.get("pending_deliveries", 0)),
            "pending_obligations": int(self.indicators.get("pending_obligations", 0)),
            "held_compression_locks": int(
                self.indicators.get("held_compression_locks", 0)
            ),
            "recently_active_sessions": int(
                self.indicators.get("recently_active_sessions", 0)
            ),
            "row_contents_read": False,
            "identifiers_read": False,
            "blockers": list(self.blockers),
        }


def _unmeasurable(code: str) -> ControlActivityReport:
    return ControlActivityReport(
        status=STATUS_UNMEASURABLE, indicators={}, blockers=(code,)
    )


def _introspect(connection: sqlite3.Connection) -> str | None:
    """Return a blocker code when the schema cannot support the guard."""
    try:
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    except sqlite3.Error:
        return "CONTROL_DB_QUERY_FAILED"
    for table, columns in REQUIRED_TABLES.items():
        if table not in present:
            return "CONTROL_SCHEMA_TABLE_MISSING"
        try:
            info = list(connection.execute(f'PRAGMA table_info("{table}")'))
        except sqlite3.Error:
            return "CONTROL_DB_QUERY_FAILED"
        if not info:
            return "CONTROL_SCHEMA_TABLE_MISSING"
        try:
            names = {str(row[1]) for row in info}
        except (IndexError, TypeError):  # pragma: no cover - defensive
            return "CONTROL_SCHEMA_UNEXPECTED_TYPE"
        if not set(columns).issubset(names):
            return "CONTROL_SCHEMA_COLUMN_MISSING"
    return None


def _count_in(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: tuple[str, ...],
) -> int:
    marks = ",".join("?" for _ in values)
    row = connection.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE {column} IN ({marks})',
        values,
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("count_unavailable")
    return int(row[0])


def evaluate_control_activity(
    state_db: str,
    *,
    recent_activity_seconds: int = DEFAULT_RECENT_ACTIVITY_SECONDS,
    now: float | None = None,
) -> ControlActivityReport:
    """Evaluate live control activity strictly read-only and fail-closed."""
    path = Path(state_db)
    if not path.is_file():
        return _unmeasurable("CONTROL_DB_UNREADABLE")
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(
            uri, uri=True, timeout=_SQLITE_TIMEOUT_SECONDS
        )
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        return _unmeasurable("CONTROL_DB_UNREADABLE")

    try:
        blocker = _introspect(connection)
        if blocker is not None:
            return _unmeasurable(blocker)

        moment = time.time() if now is None else float(now)
        cutoff = moment - max(0, int(recent_activity_seconds))
        try:
            indicators = {
                "active_delegations": _count_in(
                    connection, "async_delegations", "state", ACTIVE_DELEGATION_STATES
                ),
                "pending_deliveries": _count_in(
                    connection,
                    "async_delegations",
                    "delivery_state",
                    ACTIVE_DELIVERY_STATES,
                ),
                "pending_obligations": _count_in(
                    connection,
                    "delivery_obligations",
                    "state",
                    ACTIVE_OBLIGATION_STATES,
                ),
            }
            row = connection.execute(
                "SELECT COUNT(*) FROM compression_locks "
                "WHERE expires_at IS NULL OR expires_at > ?",
                (moment,),
            ).fetchone()
            indicators["held_compression_locks"] = int(row[0]) if row else 0
            row = connection.execute(
                "SELECT COUNT(*) FROM sessions "
                "WHERE last_activity_at IS NOT NULL AND last_activity_at >= ?",
                (cutoff,),
            ).fetchone()
            indicators["recently_active_sessions"] = int(row[0]) if row else 0
        except (sqlite3.Error, TypeError, ValueError):
            return _unmeasurable("CONTROL_DB_QUERY_FAILED")
    finally:
        connection.close()

    status = STATUS_ACTIVE if any(indicators.values()) else STATUS_QUIET
    return ControlActivityReport(status=status, indicators=indicators, blockers=())


__all__ = [
    "ACTIVE_DELEGATION_STATES",
    "ACTIVE_DELIVERY_STATES",
    "ACTIVE_OBLIGATION_STATES",
    "BLOCKER_CODES",
    "CONTROL_ACTIVITY_SCHEMA",
    "DEFAULT_RECENT_ACTIVITY_SECONDS",
    "REQUIRED_TABLES",
    "STATUS_ACTIVE",
    "STATUS_QUIET",
    "STATUS_UNMEASURABLE",
    "ControlActivityReport",
    "evaluate_control_activity",
]
