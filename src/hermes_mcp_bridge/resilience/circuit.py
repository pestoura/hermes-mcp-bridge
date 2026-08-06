"""Minimal per-upstream circuit breaker (closed / open / half-open).

The breaker is deliberately small: no background threads, no timers, no
external dependencies. State transitions are driven by explicit calls plus an
injected :class:`~hermes_mcp_bridge.resilience.clock.Clock`, which makes every
transition deterministic under test.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum

from .clock import Clock, MonotonicClock

#: Endpoint/upstream names longer than this are truncated for metric labels.
MAX_NAME_CHARS = 32


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the circuit is open."""


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int = 5
    recovery_seconds: float = 30.0
    half_open_max_calls: int = 1
    success_threshold: int = 1

    def __post_init__(self) -> None:
        if not (1 <= self.failure_threshold <= 1000):
            raise ValueError("failure_threshold must be in [1, 1000]")
        if not (0.0 < self.recovery_seconds <= 3600.0):
            raise ValueError("recovery_seconds must be in (0, 3600]")
        if not (1 <= self.half_open_max_calls <= 100):
            raise ValueError("half_open_max_calls must be in [1, 100]")
        if not (1 <= self.success_threshold <= 100):
            raise ValueError("success_threshold must be in [1, 100]")


class CircuitBreaker:
    """Thread-safe circuit breaker for a single logical upstream."""

    def __init__(
        self,
        name: str,
        *,
        config: CircuitBreakerConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.name = str(name)[:MAX_NAME_CHARS] or "upstream"
        self.config = config or CircuitBreakerConfig()
        self._clock = clock or MonotonicClock()
        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._successes = 0
        self._half_open_calls = 0
        self._opened_at = 0.0
        self.transitions = 0
        self.rejections = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def snapshot(self) -> dict[str, object]:
        """Sanitized state snapshot (no identifiers, no payloads)."""

        with self._lock:
            self._maybe_half_open()
            return {
                "name": self.name,
                "state": self._state.value,
                "failures": self._failures,
                "successes": self._successes,
                "transitions": self.transitions,
                "rejections": self.rejections,
            }

    def allows(self) -> bool:
        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.OPEN:
                return False
            if self._state is CircuitState.HALF_OPEN:
                return self._half_open_calls < self.config.half_open_max_calls
            return True

    def acquire(self) -> None:
        """Reserve a call slot or raise :class:`CircuitOpenError`."""

        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.OPEN or (
                self._state is CircuitState.HALF_OPEN
                and self._half_open_calls >= self.config.half_open_max_calls
            ):
                self.rejections += 1
                _record_rejection(self.name)
                raise CircuitOpenError(f"circuit open for {self.name}")
            if self._state is CircuitState.HALF_OPEN:
                self._half_open_calls += 1

    def record_success(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._successes += 1
                self._half_open_calls = max(0, self._half_open_calls - 1)
                if self._successes >= self.config.success_threshold:
                    self._transition(CircuitState.CLOSED)
                return
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._half_open_calls = max(0, self._half_open_calls - 1)
                self._transition(CircuitState.OPEN)
                return
            self._failures += 1
            if self._failures >= self.config.failure_threshold:
                self._transition(CircuitState.OPEN)

    def reset(self) -> None:
        with self._lock:
            self._transition(CircuitState.CLOSED)

    def _maybe_half_open(self) -> None:
        if self._state is not CircuitState.OPEN:
            return
        if self._clock.now() - self._opened_at >= self.config.recovery_seconds:
            self._transition(CircuitState.HALF_OPEN)

    def _transition(self, target: CircuitState) -> None:
        if target is self._state:
            return
        self._state = target
        self.transitions += 1
        self._failures = 0
        self._successes = 0
        self._half_open_calls = 0
        if target is CircuitState.OPEN:
            self._opened_at = self._clock.now()
        _record_transition(self.name, target.value)


def _record_transition(name: str, state: str) -> None:
    from ..observability.instrumentation import record_circuit_transition

    record_circuit_transition(name=name, state=state)


def _record_rejection(name: str) -> None:
    from ..observability.instrumentation import record_circuit_rejection

    record_circuit_rejection(name=name)


_registry: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(
    name: str,
    *,
    config: CircuitBreakerConfig | None = None,
    clock: Clock | None = None,
) -> CircuitBreaker:
    """Return (and memoize) a process-wide breaker for ``name``."""

    key = str(name)[:MAX_NAME_CHARS] or "upstream"
    with _registry_lock:
        breaker = _registry.get(key)
        if breaker is None:
            breaker = CircuitBreaker(key, config=config, clock=clock)
            _registry[key] = breaker
        return breaker


def breaker_snapshots() -> list[dict[str, object]]:
    """Return sorted, sanitized snapshots of all process-wide breakers."""

    with _registry_lock:
        breakers = list(_registry.values())
    return sorted((breaker.snapshot() for breaker in breakers), key=lambda item: str(item["name"]))


def reset_breakers() -> None:
    """Clear the process-wide breaker registry (tests / restart paths)."""

    with _registry_lock:
        _registry.clear()
