"""Idempotent run-state tracking across SSE and polling.

The tracker is the single point that decides whether an observed run event is
new information. It guarantees:

* terminal states never regress to a non-terminal state;
* the same terminal state is applied at most once (no double completion);
* duplicate and out-of-order events are ignored, not replayed;
* SSE→polling fallback cannot double-count a completion, because both paths
  funnel through :meth:`RunStateTracker.observe`.

No prompt/output text is stored: only status, an ordering sequence and a
truncated fingerprint of the run identifier.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
_ORDER: dict[str, int] = {
    "unknown": 0,
    "queued": 1,
    "running": 2,
    "completed": 3,
    "failed": 3,
    "cancelled": 3,
}


class TerminalStateError(RuntimeError):
    """Raised when a terminal state would be contradicted by a later one."""


def fingerprint(value: str | None) -> str:
    """Short, non-reversible fingerprint safe for logs and reports."""

    if not value:
        return "none"
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


@dataclass
class RunObservation:
    status: str
    sequence: int
    source: str
    applied: bool
    reason: str = ""


@dataclass
class _RunState:
    status: str = "unknown"
    sequence: int = -1
    terminal_applied: bool = False
    duplicates: int = 0
    out_of_order: int = 0
    sources: set[str] = field(default_factory=set)


class RunStateTracker:
    """Thread-safe idempotent state machine for one bridge process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, _RunState] = {}

    def observe(
        self,
        run_id: str,
        status: str,
        *,
        sequence: int | None = None,
        source: str = "sse",
    ) -> RunObservation:
        """Record an observation and report whether it changed state."""

        from ..observability.instrumentation import (
            record_duplicate_event,
            record_out_of_order_event,
        )

        normalized = str(status or "unknown").strip().lower()
        if normalized not in _ORDER:
            normalized = "unknown"
        origin = str(source or "unknown").strip().lower()[:16]

        with self._lock:
            state = self._runs.setdefault(run_id, _RunState())
            state.sources.add(origin)
            seq = state.sequence + 1 if sequence is None else int(sequence)

            if state.terminal_applied:
                if normalized == state.status:
                    state.duplicates += 1
                    record_duplicate_event(source=origin)
                    return RunObservation(state.status, state.sequence, origin, False, "duplicate")
                if normalized in TERMINAL_STATES:
                    raise TerminalStateError(
                        f"conflicting terminal state for run {fingerprint(run_id)}"
                    )
                state.out_of_order += 1
                record_out_of_order_event(source=origin)
                return RunObservation(state.status, state.sequence, origin, False, "terminal_lock")

            if sequence is not None and seq <= state.sequence:
                if normalized == state.status:
                    state.duplicates += 1
                    record_duplicate_event(source=origin)
                    reason = "duplicate"
                else:
                    state.out_of_order += 1
                    record_out_of_order_event(source=origin)
                    reason = "out_of_order"
                return RunObservation(state.status, state.sequence, origin, False, reason)

            if _ORDER[normalized] < _ORDER[state.status]:
                state.out_of_order += 1
                record_out_of_order_event(source=origin)
                return RunObservation(state.status, state.sequence, origin, False, "regression")

            if normalized == state.status and normalized not in TERMINAL_STATES:
                state.duplicates += 1
                state.sequence = max(state.sequence, seq)
                record_duplicate_event(source=origin)
                return RunObservation(state.status, state.sequence, origin, False, "duplicate")

            state.status = normalized
            state.sequence = max(state.sequence, seq)
            if normalized in TERMINAL_STATES:
                state.terminal_applied = True
            return RunObservation(state.status, state.sequence, origin, True, "applied")

    def status(self, run_id: str) -> str:
        with self._lock:
            state = self._runs.get(run_id)
            return state.status if state else "unknown"

    def is_terminal(self, run_id: str) -> bool:
        with self._lock:
            state = self._runs.get(run_id)
            return bool(state and state.terminal_applied)

    def stats(self, run_id: str) -> dict[str, object]:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return {"status": "unknown", "duplicates": 0, "out_of_order": 0, "sources": []}
            return {
                "status": state.status,
                "duplicates": state.duplicates,
                "out_of_order": state.out_of_order,
                "sources": sorted(state.sources),
            }

    def forget(self, run_id: str) -> None:
        """Release tracker resources for a run (cancellation / cleanup)."""

        with self._lock:
            self._runs.pop(run_id, None)

    def size(self) -> int:
        with self._lock:
            return len(self._runs)
