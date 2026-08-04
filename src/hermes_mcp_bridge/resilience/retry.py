"""Bounded retry for transient SQLite contention.

Only ``sqlite3.OperationalError`` whose message indicates a locked/busy
database is retried. Everything else propagates immediately: silently retrying
logic errors would hide bugs. The loop is bounded by
:attr:`BackoffPolicy.max_attempts` — there is no unbounded ``while True``.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from .backoff import BackoffPolicy
from .clock import Clock, MonotonicClock

T = TypeVar("T")

_TRANSIENT_MARKERS = ("database is locked", "database table is locked", "busy")


class RetryExhaustedError(RuntimeError):
    """Raised when every bounded retry attempt hit transient contention."""


def is_transient_sqlite_error(error: BaseException) -> bool:
    if not isinstance(error, sqlite3.OperationalError):
        return False
    message = str(error).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry configuration for SQLite operations."""

    backoff: BackoffPolicy = field(
        # BackoffPolicy is frozen and hashable, but ruff (RUF009) rightly flags
        # call-in-default; use a default_factory so the intent stays explicit.
        default_factory=lambda: BackoffPolicy(
            base_seconds=0.01,
            multiplier=2.0,
            max_seconds=0.5,
            max_attempts=5,
            jitter_ratio=0.25,
        )
    )
    kind: str = "state"


def run_with_retry(
    operation: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    clock: Clock | None = None,
    rng: random.Random | None = None,
) -> T:
    """Run ``operation`` retrying only transient SQLite contention.

    Emits contention/retry metrics per retried attempt. Returns the operation
    result or raises the original error (non-transient) / ``RetryExhaustedError``.
    """

    from ..observability.instrumentation import record_sqlite_retry

    resolved = policy or RetryPolicy()
    active_clock = clock or MonotonicClock()
    attempts = resolved.backoff.max_attempts
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except sqlite3.OperationalError as error:
            if not is_transient_sqlite_error(error):
                raise
            last_error = error
            if attempt >= attempts:
                break
            record_sqlite_retry(kind=resolved.kind)
            active_clock.sleep(resolved.backoff.delay(attempt, rng=rng))

    raise RetryExhaustedError(
        f"sqlite operation '{resolved.kind}' still contended after {attempts} attempts"
    ) from last_error
