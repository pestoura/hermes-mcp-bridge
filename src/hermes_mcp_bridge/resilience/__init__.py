"""Resilience primitives for the Hermes MCP bridge (Block 3).

Submodules:

* :mod:`.clock` — injectable time abstractions (deterministic tests).
* :mod:`.backoff` — bounded exponential backoff with controllable jitter and
  ``Retry-After`` parsing.
* :mod:`.circuit` — a minimal per-upstream circuit breaker
  (closed/open/half-open) with bounded configuration and metrics.
* :mod:`.retry` — bounded retry for transient SQLite contention.
* :mod:`.events` — idempotent run-state tracking for SSE/polling convergence.
* :mod:`.recovery` — post-crash state recovery helpers.

Nothing here performs network or filesystem I/O by itself, and no module
imports test-only helpers.
"""

from __future__ import annotations

from .backoff import BackoffPolicy, parse_retry_after
from .circuit import CircuitBreaker, CircuitBreakerConfig, CircuitOpenError, CircuitState
from .clock import Clock, ManualClock, MonotonicClock
from .events import RunStateTracker, TerminalStateError
from .recovery import RecoveryReport, recover_state
from .retry import RetryExhaustedError, RetryPolicy, run_with_retry

__all__ = [
    "BackoffPolicy",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitOpenError",
    "CircuitState",
    "Clock",
    "ManualClock",
    "MonotonicClock",
    "RecoveryReport",
    "RetryExhaustedError",
    "RetryPolicy",
    "RunStateTracker",
    "TerminalStateError",
    "parse_retry_after",
    "recover_state",
    "run_with_retry",
]
