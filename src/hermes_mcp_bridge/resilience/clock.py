"""Injectable clocks so backoff/circuit logic is testable without sleeps."""

from __future__ import annotations

import threading
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Minimal monotonic clock contract."""

    def now(self) -> float:  # pragma: no cover - protocol
        ...

    def sleep(self, seconds: float) -> None:  # pragma: no cover - protocol
        ...


class MonotonicClock:
    """Real clock backed by :func:`time.monotonic`."""

    __slots__ = ()

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class ManualClock:
    """Deterministic clock for tests: time only moves when advanced.

    ``sleep`` never blocks; it advances virtual time and records the request so
    tests can assert on the backoff schedule.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._lock = threading.Lock()
        self.sleeps: list[float] = []

    def now(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        amount = max(0.0, float(seconds))
        with self._lock:
            self.sleeps.append(amount)
            self._now += amount

    def advance(self, seconds: float) -> float:
        with self._lock:
            self._now += max(0.0, float(seconds))
            return self._now

    @property
    def total_slept(self) -> float:
        with self._lock:
            return sum(self.sleeps)
