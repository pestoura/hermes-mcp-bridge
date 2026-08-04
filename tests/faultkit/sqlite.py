"""SQLite fault injection: transient busy, hard errors and disk-write failure."""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class InjectedDiskFullError(sqlite3.OperationalError):
    """Simulated ``disk I/O error`` raised on write statements."""


@dataclass
class FaultySqlite:
    """Deterministic wrapper deciding when a SQLite call should fail.

    ``failures`` is the exact number of consecutive injected failures before
    calls start succeeding, which makes bounded-retry assertions exact. When
    ``rate`` is set instead, failures are drawn from a seeded RNG.
    """

    failures: int = 0
    rate: float = 0.0
    seed: int = 4242
    error: str = "database is locked"
    calls: int = 0
    injected: int = 0

    def __post_init__(self) -> None:
        if not (0.0 <= self.rate <= 1.0):
            raise ValueError("rate must be in [0, 1]")
        self._rng = random.Random(self.seed)

    def should_fail(self) -> bool:
        self.calls += 1
        if self.injected < self.failures:
            self.injected += 1
            return True
        if self.rate and self._rng.random() < self.rate:
            self.injected += 1
            return True
        return False

    def raise_if_failing(self) -> None:
        if self.should_fail():
            raise sqlite3.OperationalError(self.error)

    def wrap(self, operation: Callable[[], object]) -> Callable[[], object]:
        def runner() -> object:
            self.raise_if_failing()
            return operation()

        return runner


class _ConnectionProxy:
    """Thin proxy exposing a patched ``execute`` over a real connection.

    ``sqlite3.Connection.execute`` is read-only, so faults are injected through
    a proxy rather than by mutating the connection object.
    """

    def __init__(self, connection: sqlite3.Connection, execute: Callable[..., sqlite3.Cursor]):
        self._connection = connection
        self._execute = execute

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._execute(sql, *args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._connection, item)


def flaky_connection(db_path: str, faults: FaultySqlite) -> Any:
    """Open a connection whose ``execute`` fails per the fault plan.

    Only the proxy handed back here is affected — no global patching.
    """

    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.isolation_level = None
    original = connection.execute

    def execute(sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        if faults.should_fail():
            raise sqlite3.OperationalError(faults.error)
        return original(sql, *args, **kwargs)

    return _ConnectionProxy(connection, execute)


def disk_full_connection(db_path: str, *, fail_after: int = 1) -> Any:
    """Connection proxy that raises a simulated disk write failure on writes.

    ``fail_after`` write statements succeed before the failure is injected, so
    a test can crash a transaction mid-persistence deterministically.
    """

    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.isolation_level = None
    original = connection.execute
    state = {"writes": 0}
    write_prefixes = ("insert", "update", "delete", "replace")

    def execute(sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        statement = str(sql).strip().lower()
        if statement.startswith(write_prefixes):
            state["writes"] += 1
            if state["writes"] > fail_after:
                raise InjectedDiskFullError("disk I/O error")
        return original(sql, *args, **kwargs)

    return _ConnectionProxy(connection, execute)
