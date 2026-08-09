"""Phase 9 lifecycle: in-flight tracking, graceful drain, orphan sweep.

> **V2 · PHASE 9 · hardening, applies to the bridge's own HTTP runner**

The MCP bridge is a long-running server. When it is stopped (deploy, config
change, fault), any in-flight tool call must be allowed to finish within a
bounded window, and anything still stuck after that window must be swept — not
left as an orphan process that only a manual restart clears. This module is the
corrected drain primitive: it is exactly the invariant the upstream gateway
defect violated (a completed turn kept its agent pinned, so the drain waited on
a ghost and required a manual restart).

The registry is short-lived by construction: a task is added the moment it
starts and removed the moment it ends, in a `finally`. A count of zero after a
completed turn is the load-bearing assertion in the regression suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

logger = logging.getLogger("hermes_mcp_bridge.lifecycle")

DEFAULT_GRACE_SECONDS: float = 30.0
DEFAULT_SWEEP_TIMEOUT_SECONDS: float = 5.0


class InFlightRegistry:
    """Tracks live unit-of-work tasks and proves they reach zero.

    Mirrors the upstream gateway contract that was defective: ``_active_run_tasks``
    plus ``_shutdown_interruptible_agents`` must both empty the instant a turn
    finishes. Here the single registry is emptied in a ``finally`` on every path,
    so ``live_count == 0`` is guaranteed after completion and no drain can wait
    on a ghost.
    """

    __slots__ = ("_tasks", "_total_admitted", "_total_completed")

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._total_admitted: int = 0
        self._total_completed: int = 0

    def register(self, key: str, task: asyncio.Task[Any]) -> None:
        self._tasks[key] = task
        self._total_admitted += 1

        def _on_done(done: asyncio.Task[Any]) -> None:
            # The clear is unconditional and lives in the task lifetime, not in
            # the caller's happy path — this is the exact fix for the orphan
            # defect.
            self._tasks.pop(key, None)
            self._total_completed += 1

        task.add_done_callback(_on_done)

    def live_keys(self) -> list[str]:
        return [key for key, task in self._tasks.items() if not task.done()]

    def live_count(self) -> int:
        return len(self.live_keys())

    def incomplete_count(self) -> int:
        """Tasks still pending — the orphan metric the drain must never trust stale."""
        return len(self._tasks)

    def totals(self) -> dict[str, int]:
        return {
            "admitted": self._total_admitted,
            "completed": self._total_completed,
            "live": self.live_count(),
        }


async def drain_in_flight(
    registry: InFlightRegistry,
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    sweep_timeout_seconds: float = DEFAULT_SWEEP_TIMEOUT_SECONDS,
    interrupt: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Wait for in-flight work to finish, then sweep any survivor.

    Returns a structured, secret-free record. ``manual_restart_required`` is the
    load-bearing flag: it is ``False`` exactly when the drain reached zero on its
    own, which is the whole point of the correction.
    """
    started = clock()
    deadline = started + max(0.0, grace_seconds)

    while registry.live_count() > 0 and clock() < deadline:
        await asyncio.sleep(0.05)

    remaining = registry.live_keys()
    if remaining and interrupt is not None:
        for key in remaining:
            with contextlib.suppress(Exception):
                interrupt(key)

    # Sweep window: anything still registered after the grace period is an orphan
    # and must be cancelled here, so the caller never inherits a stuck task.
    sweep_deadline = clock() + max(0.0, sweep_timeout_seconds)
    while registry.incomplete_count() > 0 and clock() < sweep_deadline:
        for key in list(registry._tasks):
            task = registry._tasks[key]
            if not task.done():
                task.cancel()
        await asyncio.sleep(0.05)

    survivors = registry.live_count()
    return {
        "admitted": registry._total_admitted,
        "completed": registry._total_completed,
        "live_after_grace": len(remaining),
        "survivors_after_sweep": survivors,
        "manual_restart_required": survivors > 0,
        "waited_seconds": round(clock() - started, 3),
    }


class UnitOfWork(Protocol):
    async def __call__(self) -> Any: ...


async def run_with_registry(
    registry: InFlightRegistry,
    key: str,
    work: Callable[[], Awaitable[Any]],
) -> Any:
    """Run ``work`` tracked under ``key``; registry is cleared even on failure."""
    task = asyncio.ensure_future(work())
    registry.register(key, task)
    try:
        return await task
    except BaseException:
        # Ensure cancellation state is observed; the done-callback clears the
        # registry regardless.
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        raise


__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_SWEEP_TIMEOUT_SECONDS",
    "InFlightRegistry",
    "drain_in_flight",
    "run_with_registry",
]
