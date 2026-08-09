"""Phase 4 BATCH typed contract — envelope over N already-typed DIRECT steps.

> **V2 · PHASE 4 · runtime, disabled by default behind ``BATCH_FEATURE_ENABLED``**

A batch adds **no new capability**. Every step must resolve to an existing
typed registry entry; there is no free-form command, no shell, no subprocess and
no generic HTTP surface anywhere in the Phase 4 module set (proved by
``scripts/validate_v2_phase4_batch_gate.py``).

Design source: ``docs/v2/phase4/contract.md`` and ``limits-and-budgets.md``.
Validation is **total and pre-execution**: an envelope that violates any budget,
ordering or typing rule is ``DENIED`` with zero side effects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Any

from .canonical import canonical_json_bytes, sha256_hex
from .errors import V2Error

BATCH_SCHEMA_VERSION = "batch/1"

#: Server-side authoritative ceilings (``docs/v2/phase4/limits-and-budgets.md``).
BATCH_MAX_ITEMS = 10
BATCH_MAX_PARALLELISM = 4
BATCH_MAX_PARALLELISM_MUTATION = 1
BATCH_MAX_TIMEOUT_S = 300
BATCH_MAX_INFLIGHT_GLOBAL = 8

#: Phase 4 ships disabled. Only an explicit caller opt-in enables execution.
BATCH_FEATURE_ENABLED = False

_IDENTIFIER_MAX = 128


@unique
class BatchErrorCode(StrEnum):
    """Batch-layer error codes. Step errors keep the DIRECT taxonomy."""

    BATCH_VALIDATION_FAILED = "BATCH_VALIDATION_FAILED"
    BATCH_CAPACITY_EXHAUSTED = "BATCH_CAPACITY_EXHAUSTED"
    BATCH_TIMEOUT = "BATCH_TIMEOUT"
    BATCH_CANCELLED = "BATCH_CANCELLED"
    BATCH_DISABLED = "BATCH_DISABLED"


@unique
class BatchFailurePolicy(StrEnum):
    """Explicit, no default: the caller must choose."""

    FAIL_FAST = "fail_fast"
    CONTINUE_ON_ERROR = "continue_on_error"


@unique
class StepStatus(StrEnum):
    """Exhaustive terminal step status set. There is no ``UNKNOWN``."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DENIED = "DENIED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    NOT_STARTED = "NOT_STARTED"


@unique
class BatchStatus(StrEnum):
    """Aggregate status. ``PARTIAL`` is first-class, never an error string."""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    DENIED = "DENIED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


@unique
class IdempotencyOutcome(StrEnum):
    EXECUTED = "EXECUTED"
    REPLAYED = "REPLAYED"
    CONFLICT = "CONFLICT"


class BatchError(V2Error):
    """Base class for batch-layer errors, carrying a stable typed code."""

    def __init__(self, code: BatchErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}:{detail}" if detail else code.value)


class BatchValidationError(BatchError):
    def __init__(self, detail: str) -> None:
        super().__init__(BatchErrorCode.BATCH_VALIDATION_FAILED, detail)


class BatchCapacityError(BatchError):
    def __init__(self, detail: str = "global inflight ceiling reached") -> None:
        super().__init__(BatchErrorCode.BATCH_CAPACITY_EXHAUSTED, detail)


class BatchDisabledError(BatchError):
    def __init__(self, detail: str = "batch feature flag is off") -> None:
        super().__init__(BatchErrorCode.BATCH_DISABLED, detail)


def _require_identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchValidationError(f"{field_name}: must be a non-empty string")
    if len(value) > _IDENTIFIER_MAX:
        raise BatchValidationError(f"{field_name}: too long")
    if any(ch.isspace() for ch in value):
        raise BatchValidationError(f"{field_name}: must not contain whitespace")
    return value


def _require_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BatchValidationError(f"{field_name}: must be an int")
    if value < 1:
        raise BatchValidationError(f"{field_name}: must be >= 1")
    return value


@dataclass(frozen=True, slots=True)
class BatchStep:
    """One typed step. ``depends_on`` exists for DAG forward-compatibility."""

    step_id: str
    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    approval_ref: str | None = None
    step_timeout_s: int = 30
    depends_on: Sequence[str] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.step_id, field_name="step_id")
        _require_identifier(self.tool, field_name="tool")
        if not isinstance(self.args, Mapping):
            raise BatchValidationError("args: must be a mapping")
        for name in ("idempotency_key", "approval_ref"):
            value = getattr(self, name)
            if value is not None:
                _require_identifier(value, field_name=name)
        _require_positive_int(self.step_timeout_s, field_name="step_timeout_s")
        if not isinstance(self.depends_on, Sequence) or isinstance(self.depends_on, str):
            raise BatchValidationError("depends_on: must be a sequence")
        if len(self.depends_on) != 0:
            # Reserved for the DAG mode (Phase 5); fail closed here.
            raise BatchValidationError("depends_on: must be empty in BATCH mode")

    def digest(self) -> str:
        """Per-step digest. Approvals bind to *this*, never to the batch."""
        return sha256_hex(
            canonical_json_bytes(
                {
                    "step_id": self.step_id,
                    "tool": self.tool,
                    "args": dict(self.args),
                    "idempotency_key": self.idempotency_key,
                    "step_timeout_s": self.step_timeout_s,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class BatchRequest:
    """Validated batch envelope. Construction is the validation gate."""

    batch_id: str
    steps: Sequence[BatchStep]
    failure_policy: BatchFailurePolicy
    max_parallelism: int
    batch_timeout_s: int
    dry_run: bool = False
    schema_version: str = BATCH_SCHEMA_VERSION
    mode: str = "BATCH"

    def __post_init__(self) -> None:
        if self.schema_version != BATCH_SCHEMA_VERSION:
            raise BatchValidationError("schema_version: unknown value")
        if self.mode != "BATCH":
            raise BatchValidationError("mode: must be BATCH")
        _require_identifier(self.batch_id, field_name="batch_id")
        if not isinstance(self.failure_policy, BatchFailurePolicy):
            raise BatchValidationError("failure_policy: must be explicit")
        if not isinstance(self.dry_run, bool):
            raise BatchValidationError("dry_run: must be a bool")
        if not isinstance(self.steps, Sequence) or isinstance(self.steps, str):
            raise BatchValidationError("steps: must be a sequence")
        if not self.steps:
            raise BatchValidationError("steps: at least one step is required")
        if len(self.steps) > BATCH_MAX_ITEMS:
            raise BatchValidationError(
                f"steps: {len(self.steps)} exceeds BATCH_MAX_ITEMS={BATCH_MAX_ITEMS}"
            )
        seen: set[str] = set()
        for step in self.steps:
            if not isinstance(step, BatchStep):
                raise BatchValidationError("steps: entries must be BatchStep")
            if step.step_id in seen:
                raise BatchValidationError(f"steps: duplicate step_id {step.step_id!r}")
            seen.add(step.step_id)

        _require_positive_int(self.max_parallelism, field_name="max_parallelism")
        _require_positive_int(self.batch_timeout_s, field_name="batch_timeout_s")
        # Ceilings are never silently clamped: intent stays visible in the audit.
        if self.max_parallelism > BATCH_MAX_PARALLELISM:
            raise BatchValidationError(
                f"max_parallelism: {self.max_parallelism} exceeds ceiling {BATCH_MAX_PARALLELISM}"
            )
        if self.batch_timeout_s > BATCH_MAX_TIMEOUT_S:
            raise BatchValidationError(f"batch_timeout_s: exceeds ceiling {BATCH_MAX_TIMEOUT_S}")
        for step in self.steps:
            if step.step_timeout_s > self.batch_timeout_s:
                raise BatchValidationError(
                    f"step_timeout_s: step {step.step_id!r} exceeds batch_timeout_s"
                )

    @property
    def step_ids(self) -> tuple[str, ...]:
        return tuple(step.step_id for step in self.steps)

    def digest(self) -> str:
        """Evidence/replay digest only — never an authorization token."""
        return sha256_hex(
            canonical_json_bytes(
                {
                    "batch_id": self.batch_id,
                    "failure_policy": self.failure_policy.value,
                    "max_parallelism": self.max_parallelism,
                    "batch_timeout_s": self.batch_timeout_s,
                    "dry_run": self.dry_run,
                    "steps": [step.digest() for step in self.steps],
                }
            )
        )


@dataclass(frozen=True, slots=True)
class BatchStepResult:
    step_id: str
    status: StepStatus
    started_at: str | None = None
    finished_at: str | None = None
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    idempotency_outcome: IdempotencyOutcome | None = None
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, StepStatus):
            raise BatchValidationError("status: must be a StepStatus")
        if self.status is StepStatus.NOT_STARTED and (
            self.started_at is not None or self.result is not None
        ):
            raise BatchValidationError("NOT_STARTED step must carry no timing or result")


@dataclass(frozen=True, slots=True)
class BatchResult:
    batch_id: str
    aggregate_status: BatchStatus
    steps: Sequence[BatchStepResult]
    counts: Mapping[str, int]
    started_at: str
    finished_at: str
    evidence_ref: str
    effective_parallelism: int = 1
    max_observed_inflight: int = 0

    def __post_init__(self) -> None:
        if sum(self.counts.values()) != len(self.steps):
            raise BatchValidationError("counts: must sum to the number of steps")
        ids = [entry.step_id for entry in self.steps]
        if len(set(ids)) != len(ids):
            raise BatchValidationError("steps: duplicate step_id in result")


def count_statuses(results: Sequence[BatchStepResult]) -> dict[str, int]:
    counts = {status.value: 0 for status in StepStatus}
    for entry in results:
        counts[entry.status.value] += 1
    return {key: value for key, value in counts.items() if value}


def aggregate_status(
    results: Sequence[BatchStepResult],
    *,
    cancelled: bool = False,
    timed_out: bool = False,
) -> BatchStatus:
    """Deterministic aggregation algebra (``docs/v2/phase4/aggregation-and-evidence.md``).

    ``PARTIAL`` is first-class: any success alongside any non-success is
    ``PARTIAL``, regardless of why the batch stopped.
    """
    statuses = [entry.status for entry in results]
    successes = sum(1 for status in statuses if status is StepStatus.SUCCESS)
    if successes == len(statuses) and statuses:
        return BatchStatus.SUCCESS
    if successes:
        return BatchStatus.PARTIAL
    if timed_out:
        return BatchStatus.TIMED_OUT
    if statuses and all(status is StepStatus.DENIED for status in statuses):
        return BatchStatus.DENIED
    # No success and nothing concretely failed: a deadline ended the batch.
    stopped = {StepStatus.TIMED_OUT, StepStatus.CANCELLED, StepStatus.NOT_STARTED}
    if statuses and all(status in stopped for status in statuses):
        if any(status is StepStatus.TIMED_OUT for status in statuses):
            return BatchStatus.TIMED_OUT
        return BatchStatus.CANCELLED
    # A concrete step failure outranks the cancellation that it triggered:
    # fail_fast with no successes is FAILED, per failure-and-cancellation.md.
    if any(
        status in (StepStatus.FAILED, StepStatus.DENIED, StepStatus.TIMED_OUT)
        for status in statuses
    ):
        return BatchStatus.FAILED
    if cancelled:
        return BatchStatus.CANCELLED
    return BatchStatus.FAILED


__all__ = [
    "BATCH_FEATURE_ENABLED",
    "BATCH_MAX_INFLIGHT_GLOBAL",
    "BATCH_MAX_ITEMS",
    "BATCH_MAX_PARALLELISM",
    "BATCH_MAX_PARALLELISM_MUTATION",
    "BATCH_MAX_TIMEOUT_S",
    "BATCH_SCHEMA_VERSION",
    "BatchCapacityError",
    "BatchDisabledError",
    "BatchError",
    "BatchErrorCode",
    "BatchFailurePolicy",
    "BatchRequest",
    "BatchResult",
    "BatchStatus",
    "BatchStep",
    "BatchStepResult",
    "BatchValidationError",
    "IdempotencyOutcome",
    "StepStatus",
    "aggregate_status",
    "count_statuses",
]
