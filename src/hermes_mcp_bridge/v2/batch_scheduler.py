"""Phase 4 BATCH scheduler — bounded-concurrency execution over DIRECT steps.

> **V2 · PHASE 4 · runtime, disabled by default behind ``BATCH_FEATURE_ENABLED``**

Properties this module is required to hold (design:
``docs/v2/phase4/concurrency-and-scheduling.md``, ``failure-and-cancellation.md``):

* **Real parallelism.** Independent read steps execute concurrently. A serial
  implementation deadlocks the barrier test in
  ``tests/test_v2_phase4_batch_scheduler.py`` (S-01) — this is an acceptance
  requirement, not an optimisation.
* **Bounded admission.** Concurrency is limited by two bounded semaphores (per
  batch and process-global). There is no unbounded queue and no unbounded task
  spawning; the global ceiling rejects at admission with
  ``BATCH_CAPACITY_EXHAUSTED`` instead of queueing.
* **Result order is input order** regardless of completion order.
* **Fail-closed cancellation.** Admission is closed *before* in-flight steps are
  cancelled, so no step can start after the cancellation decision. There are no
  implicit retries and no rollback/compensation.
* **Per-step governance.** Policy/approval/idempotency/audit are decided once
  per step through the injected governance hook. The batch envelope is never a
  single authorization decision for mutations.

No shell, subprocess, ``eval``/``exec`` or generic HTTP client appears here: the
scheduler only calls the injected step executor.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from .batch_contract import (
    BATCH_MAX_INFLIGHT_GLOBAL,
    BATCH_MAX_PARALLELISM,
    BATCH_MAX_PARALLELISM_MUTATION,
    BatchCapacityError,
    BatchDisabledError,
    BatchFailurePolicy,
    BatchRequest,
    BatchResult,
    BatchStatus,
    BatchStep,
    BatchStepResult,
    IdempotencyOutcome,
    StepStatus,
    aggregate_status,
    count_statuses,
)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class StepDecision:
    """Outcome of per-step governance, produced before the step may execute."""

    allowed: bool
    reason: str = ""
    audit_ref: str | None = None
    idempotency_outcome: IdempotencyOutcome | None = None
    mutates: bool = False


@runtime_checkable
class StepGovernance(Protocol):
    """Per-step policy/approval/idempotency/audit. Never batch-level."""

    def decide(self, request: BatchRequest, step: BatchStep) -> StepDecision: ...

    def record(
        self, request: BatchRequest, step: BatchStep, result: BatchStepResult
    ) -> None: ...


class AllowAllGovernance:
    """Test/dry-run default. Allows read steps, records nothing external."""

    def decide(self, request: BatchRequest, step: BatchStep) -> StepDecision:
        return StepDecision(allowed=True, audit_ref=f"audit:{request.batch_id}:{step.step_id}")

    def record(
        self, request: BatchRequest, step: BatchStep, result: BatchStepResult
    ) -> None:
        return None


#: The injected executor: it runs exactly one already-typed DIRECT step.
StepExecutor = Callable[[BatchStep], Awaitable[Mapping[str, Any]]]


class _InflightMeter:
    """Samples true concurrency; ``max_observed`` is evidence, not a metric."""

    __slots__ = ("current", "max_observed")

    def __init__(self) -> None:
        self.current = 0
        self.max_observed = 0

    def enter(self) -> None:
        self.current += 1
        self.max_observed = max(self.max_observed, self.current)

    def leave(self) -> None:
        self.current -= 1


class GlobalCapacity:
    """Process-wide bounded ceiling on concurrent batch steps."""

    def __init__(self, limit: int = BATCH_MAX_INFLIGHT_GLOBAL) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self.limit = limit
        self._used = 0

    def reserve(self, amount: int) -> None:
        if self._used + amount > self.limit:
            raise BatchCapacityError(
                f"requested {amount}, used {self._used}, limit {self.limit}"
            )
        self._used += amount

    def release(self, amount: int) -> None:
        self._used = max(0, self._used - amount)


def effective_parallelism(request: BatchRequest, *, has_mutation: bool) -> int:
    """Ceiling-clamped concurrency. Any mutation step serialises the batch."""
    ceiling = BATCH_MAX_PARALLELISM_MUTATION if has_mutation else BATCH_MAX_PARALLELISM
    return max(1, min(request.max_parallelism, ceiling))


class BatchScheduler:
    """Owns one batch execution. Not reused across batches."""

    def __init__(
        self,
        executor: StepExecutor,
        *,
        governance: StepGovernance | None = None,
        capacity: GlobalCapacity | None = None,
        enabled: bool = False,
    ) -> None:
        self._executor = executor
        self._governance = governance or AllowAllGovernance()
        self._capacity = capacity or GlobalCapacity()
        self._enabled = enabled

    async def run(self, request: BatchRequest) -> BatchResult:
        if not self._enabled:
            raise BatchDisabledError()

        started_at = _now_iso()
        decisions: dict[str, StepDecision] = {
            step.step_id: self._governance.decide(request, step) for step in request.steps
        }
        has_mutation = any(decision.mutates for decision in decisions.values())
        parallelism = effective_parallelism(request, has_mutation=has_mutation)

        if request.dry_run:
            results = [
                BatchStepResult(
                    step_id=step.step_id,
                    status=(
                        StepStatus.NOT_STARTED
                        if decisions[step.step_id].allowed
                        else StepStatus.DENIED
                    ),
                    error=(
                        None
                        if decisions[step.step_id].allowed
                        else {"code": "DENIED", "reason": decisions[step.step_id].reason}
                    ),
                )
                for step in request.steps
            ]
            return self._finalize(
                request, results, started_at, parallelism, 0, cancelled=False, timed_out=False
            )

        self._capacity.reserve(parallelism)
        try:
            return await self._execute(
                request, decisions, parallelism, started_at
            )
        finally:
            self._capacity.release(parallelism)

    async def _execute(
        self,
        request: BatchRequest,
        decisions: Mapping[str, StepDecision],
        parallelism: int,
        started_at: str,
    ) -> BatchResult:
        admission = asyncio.Semaphore(parallelism)
        meter = _InflightMeter()
        # Fail-closed cancellation: closing admission strictly precedes cancelling.
        admission_closed = asyncio.Event()
        results: dict[str, BatchStepResult] = {}
        cancelled = False
        timed_out = False

        async def run_step(step: BatchStep) -> None:
            decision = decisions[step.step_id]
            if not decision.allowed:
                results[step.step_id] = BatchStepResult(
                    step_id=step.step_id,
                    status=StepStatus.DENIED,
                    error={"code": "DENIED", "reason": decision.reason},
                    audit_ref=decision.audit_ref,
                )
                return
            if admission_closed.is_set():
                results[step.step_id] = BatchStepResult(
                    step_id=step.step_id, status=StepStatus.NOT_STARTED
                )
                return
            async with admission:
                # Re-check after acquiring: admission may have closed while waiting.
                if admission_closed.is_set():
                    results[step.step_id] = BatchStepResult(
                        step_id=step.step_id, status=StepStatus.NOT_STARTED
                    )
                    return
                meter.enter()
                step_started = _now_iso()
                try:
                    payload = await asyncio.wait_for(
                        self._executor(step), timeout=step.step_timeout_s
                    )
                except TimeoutError:
                    results[step.step_id] = BatchStepResult(
                        step_id=step.step_id,
                        status=StepStatus.TIMED_OUT,
                        started_at=step_started,
                        finished_at=_now_iso(),
                        error={"code": "STEP_TIMEOUT"},
                        audit_ref=decision.audit_ref,
                    )
                except asyncio.CancelledError:
                    # Indeterminate at the bridge: never assumed failed, never
                    # compensated. Recorded and re-raised for correct teardown.
                    results[step.step_id] = BatchStepResult(
                        step_id=step.step_id,
                        status=StepStatus.CANCELLED,
                        started_at=step_started,
                        finished_at=_now_iso(),
                        error={"code": "BATCH_CANCELLED", "outcome": "INDETERMINATE"},
                        idempotency_outcome=decision.idempotency_outcome,
                        audit_ref=decision.audit_ref,
                    )
                    raise
                except Exception as exc:
                    results[step.step_id] = BatchStepResult(
                        step_id=step.step_id,
                        status=StepStatus.FAILED,
                        started_at=step_started,
                        finished_at=_now_iso(),
                        error={"code": "STEP_FAILED", "type": type(exc).__name__},
                        audit_ref=decision.audit_ref,
                    )
                else:
                    results[step.step_id] = BatchStepResult(
                        step_id=step.step_id,
                        status=StepStatus.SUCCESS,
                        started_at=step_started,
                        finished_at=_now_iso(),
                        result=dict(payload),
                        idempotency_outcome=decision.idempotency_outcome,
                        audit_ref=decision.audit_ref,
                    )
                finally:
                    meter.leave()
            entry = results.get(step.step_id)
            if entry is not None:
                self._governance.record(request, step, entry)
            if (
                request.failure_policy is BatchFailurePolicy.FAIL_FAST
                and entry is not None
                and entry.status is not StepStatus.SUCCESS
            ):
                admission_closed.set()

        tasks = [asyncio.create_task(run_step(step)) for step in request.steps]
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=request.batch_timeout_s,
            )
        except TimeoutError:
            timed_out = True
        finally:
            if any(not task.done() for task in tasks):
                admission_closed.set()  # close admission BEFORE cancelling
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        if admission_closed.is_set() and not timed_out:
            cancelled = any(
                results.get(step_id) is None
                or results[step_id].status is StepStatus.NOT_STARTED
                for step_id in request.step_ids
            )

        ordered = [
            results.get(step.step_id)
            or BatchStepResult(step_id=step.step_id, status=StepStatus.NOT_STARTED)
            for step in request.steps
        ]
        return self._finalize(
            request,
            ordered,
            started_at,
            parallelism,
            meter.max_observed,
            cancelled=cancelled,
            timed_out=timed_out,
        )

    def _finalize(
        self,
        request: BatchRequest,
        results: Sequence[BatchStepResult],
        started_at: str,
        parallelism: int,
        max_observed_inflight: int,
        *,
        cancelled: bool,
        timed_out: bool,
    ) -> BatchResult:
        return BatchResult(
            batch_id=request.batch_id,
            aggregate_status=self._aggregate(results, cancelled, timed_out, request),
            steps=tuple(results),
            counts=count_statuses(results),
            started_at=started_at,
            finished_at=_now_iso(),
            evidence_ref=f"batch-evidence:{request.batch_id}:{request.digest()[:16]}",
            effective_parallelism=parallelism,
            max_observed_inflight=max_observed_inflight,
        )

    @staticmethod
    def _aggregate(
        results: Sequence[BatchStepResult],
        cancelled: bool,
        timed_out: bool,
        request: BatchRequest,
    ) -> BatchStatus:
        if request.dry_run and all(
            entry.status is StepStatus.NOT_STARTED for entry in results
        ):
            return BatchStatus.SUCCESS
        return aggregate_status(results, cancelled=cancelled, timed_out=timed_out)


__all__ = [
    "AllowAllGovernance",
    "BatchScheduler",
    "GlobalCapacity",
    "StepDecision",
    "StepExecutor",
    "StepGovernance",
    "effective_parallelism",
]
