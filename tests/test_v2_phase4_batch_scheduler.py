"""Phase 4 BATCH acceptance scenarios S-01..S-27.

Every scenario runs offline with an injected fake step executor. No provider
calls, no network, no wall-clock sleeps beyond short barriers. A skipped or
missing scenario is a gate failure, not a skip (see
``scripts/validate_v2_phase4_batch_gate.py``).
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from hermes_mcp_bridge.v2.batch_contract import (
    BATCH_MAX_INFLIGHT_GLOBAL,
    BATCH_MAX_ITEMS,
    BATCH_MAX_PARALLELISM,
    BATCH_MAX_TIMEOUT_S,
    BatchFailurePolicy,
    BatchRequest,
    BatchStatus,
    BatchStep,
    BatchValidationError,
    IdempotencyOutcome,
    StepStatus,
)
from hermes_mcp_bridge.v2.batch_scheduler import (
    BatchScheduler,
    GlobalCapacity,
    StepDecision,
    effective_parallelism,
)

READ_TOOL = "github.get_repo"
MUTATION_TOOL = "github.create_branch"


def step(step_id: str, *, tool: str = READ_TOOL, timeout: int = 30, **kwargs: Any) -> BatchStep:
    return BatchStep(step_id=step_id, tool=tool, step_timeout_s=timeout, **kwargs)


def request(
    *steps: BatchStep,
    policy: BatchFailurePolicy = BatchFailurePolicy.CONTINUE_ON_ERROR,
    parallelism: int = 2,
    timeout: int = 60,
    dry_run: bool = False,
    batch_id: str = "b-1",
) -> BatchRequest:
    return BatchRequest(
        batch_id=batch_id,
        steps=steps,
        failure_policy=policy,
        max_parallelism=parallelism,
        batch_timeout_s=timeout,
        dry_run=dry_run,
    )


class RecordingExecutor:
    """Fake DIRECT executor: records every invocation, never touches network."""

    def __init__(self, *, fail: set[str] | None = None, hang: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail = fail or set()
        self._hang = hang or set()

    async def __call__(self, item: BatchStep) -> Mapping[str, Any]:
        self.calls.append(item.step_id)
        if item.step_id in self._hang:
            await asyncio.sleep(3600)
        if item.step_id in self._fail:
            raise RuntimeError("step boom")
        await asyncio.sleep(0)
        return {"step": item.step_id}


def run(scheduler: BatchScheduler, req: BatchRequest):
    return asyncio.run(scheduler.run(req))


def scheduler(executor, **kwargs: Any) -> BatchScheduler:
    kwargs.setdefault("enabled", True)
    return BatchScheduler(executor, **kwargs)


# --------------------------------------------------------------------------
# S-01 — non-serial execution proved by a barrier
# --------------------------------------------------------------------------


def test_s01_independent_read_steps_really_run_in_parallel() -> None:
    """A serial implementation cannot release this barrier and fails on timeout."""
    barrier = asyncio.Barrier(2)
    observed: list[int] = []
    inflight = 0

    async def executor(item: BatchStep) -> Mapping[str, Any]:
        nonlocal inflight
        inflight += 1
        observed.append(inflight)
        # Blocks until a second step is genuinely in flight at the same time.
        await asyncio.wait_for(barrier.wait(), timeout=5)
        inflight -= 1
        return {"step": item.step_id}

    req = request(*[step(f"s{i}") for i in range(4)], parallelism=2)
    result = run(scheduler(executor), req)

    assert result.aggregate_status is BatchStatus.SUCCESS
    assert result.max_observed_inflight == 2
    assert max(observed) == 2


def test_s01b_serial_executor_would_deadlock_the_barrier() -> None:
    """Guards the guard: a forced parallelism of 1 must fail the barrier."""
    barrier = asyncio.Barrier(2)

    async def executor(item: BatchStep) -> Mapping[str, Any]:
        await asyncio.wait_for(barrier.wait(), timeout=0.5)
        return {"step": item.step_id}

    req = request(step("a"), step("b"), parallelism=1)
    result = run(scheduler(executor), req)
    assert result.aggregate_status is not BatchStatus.SUCCESS
    assert all(entry.status is not StepStatus.SUCCESS for entry in result.steps)
    assert result.max_observed_inflight == 1


# --------------------------------------------------------------------------
# S-02, S-07 — ceilings and backpressure
# --------------------------------------------------------------------------


def test_s02_parallelism_ceiling_is_never_exceeded() -> None:
    executor = RecordingExecutor()
    req = request(*[step(f"s{i}") for i in range(6)], parallelism=2)
    result = run(scheduler(executor), req)
    assert result.max_observed_inflight <= 2
    assert result.effective_parallelism == 2


def test_s07_no_unbounded_inflight_under_load() -> None:
    peak = 0
    live = 0

    async def executor(item: BatchStep) -> Mapping[str, Any]:
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)
        live -= 1
        return {}

    req = request(*[step(f"s{i}") for i in range(BATCH_MAX_ITEMS)], parallelism=3)
    result = run(scheduler(executor), req)
    assert peak <= 3
    assert result.max_observed_inflight <= 3


# --------------------------------------------------------------------------
# S-03, S-04, S-19, S-24, S-25 — total pre-execution validation
# --------------------------------------------------------------------------


def test_s03_max_items_exceeded_is_denied_before_execution() -> None:
    executor = RecordingExecutor()
    with pytest.raises(BatchValidationError):
        request(*[step(f"s{i}") for i in range(BATCH_MAX_ITEMS + 1)])
    assert executor.calls == []


def test_s04_parallelism_above_ceiling_is_denied_not_clamped() -> None:
    with pytest.raises(BatchValidationError) as excinfo:
        request(step("a"), parallelism=BATCH_MAX_PARALLELISM + 1)
    assert "exceeds ceiling" in str(excinfo.value)


def test_s04b_timeout_above_ceiling_is_denied() -> None:
    with pytest.raises(BatchValidationError):
        request(step("a"), timeout=BATCH_MAX_TIMEOUT_S + 1)


def test_s04c_step_timeout_above_batch_timeout_is_denied() -> None:
    with pytest.raises(BatchValidationError):
        request(step("a", timeout=61), timeout=60)


def test_s19_batch_digest_changes_when_a_step_changes() -> None:
    first = request(step("a"), step("b"))
    second = request(step("a"), step("b", tool="github.get_pr"))
    assert first.digest() != second.digest()


def test_s24_duplicate_step_id_is_denied() -> None:
    with pytest.raises(BatchValidationError):
        request(step("a"), step("a"))


def test_s24b_unknown_envelope_field_is_rejected() -> None:
    with pytest.raises(TypeError):
        BatchRequest(  # type: ignore[call-arg]
            batch_id="b",
            steps=(step("a"),),
            failure_policy=BatchFailurePolicy.FAIL_FAST,
            max_parallelism=1,
            batch_timeout_s=10,
            unexpected=True,
        )


def test_s24c_empty_or_whitespace_identifiers_are_rejected() -> None:
    with pytest.raises(BatchValidationError):
        step("")
    with pytest.raises(BatchValidationError):
        step("bad id")


def test_s25_non_empty_depends_on_is_denied() -> None:
    with pytest.raises(BatchValidationError):
        step("a", depends_on=("b",))


# --------------------------------------------------------------------------
# S-05, S-06 — result completeness and ordering
# --------------------------------------------------------------------------


def test_s05_result_is_complete_and_counts_sum() -> None:
    executor = RecordingExecutor(fail={"s1"})
    req = request(*[step(f"s{i}") for i in range(4)])
    result = run(scheduler(executor), req)
    assert len(result.steps) == len(req.steps)
    assert sum(result.counts.values()) == len(req.steps)


def test_s06_results_follow_input_order_not_completion_order() -> None:
    delays = {"a": 0.03, "b": 0.0, "c": 0.015}

    async def executor(item: BatchStep) -> Mapping[str, Any]:
        await asyncio.sleep(delays[item.step_id])
        return {"step": item.step_id}

    req = request(step("a"), step("b"), step("c"), parallelism=3)
    result = run(scheduler(executor), req)
    assert [entry.step_id for entry in result.steps] == ["a", "b", "c"]


# --------------------------------------------------------------------------
# S-08 — global capacity, no unbounded queue
# --------------------------------------------------------------------------


def test_s08_global_capacity_rejects_at_admission() -> None:
    capacity = GlobalCapacity(limit=2)
    capacity.reserve(2)
    executor = RecordingExecutor()
    req = request(step("a"), parallelism=1)
    with pytest.raises(Exception) as excinfo:
        run(scheduler(executor, capacity=capacity), req)
    assert "BATCH_CAPACITY_EXHAUSTED" in str(excinfo.value)
    assert executor.calls == []


def test_s08b_capacity_is_released_after_a_batch() -> None:
    capacity = GlobalCapacity(limit=BATCH_MAX_INFLIGHT_GLOBAL)
    executor = RecordingExecutor()
    req = request(step("a"), parallelism=2)
    run(scheduler(executor, capacity=capacity), req)
    capacity.reserve(BATCH_MAX_INFLIGHT_GLOBAL)  # must not raise


# --------------------------------------------------------------------------
# S-09..S-15 — failure policy, cancellation, timeouts, no retries
# --------------------------------------------------------------------------


def test_s09_continue_on_error_yields_partial() -> None:
    executor = RecordingExecutor(fail={"s1"})
    req = request(*[step(f"s{i}") for i in range(4)])
    result = run(scheduler(executor), req)
    assert result.aggregate_status is BatchStatus.PARTIAL
    by_id = {entry.step_id: entry for entry in result.steps}
    assert by_id["s1"].status is StepStatus.FAILED
    assert by_id["s0"].status is StepStatus.SUCCESS


def test_s10_fail_fast_closes_admission_and_leaves_steps_not_started() -> None:
    executor = RecordingExecutor(fail={"s0"})
    req = request(
        *[step(f"s{i}") for i in range(4)],
        policy=BatchFailurePolicy.FAIL_FAST,
        parallelism=1,
    )
    result = run(scheduler(executor), req)
    statuses = {entry.step_id: entry.status for entry in result.steps}
    assert statuses["s0"] is StepStatus.FAILED
    assert StepStatus.NOT_STARTED in statuses.values()
    assert result.aggregate_status in (BatchStatus.PARTIAL, BatchStatus.FAILED)


def test_s11_step_timeout_is_isolated_under_continue_on_error() -> None:
    executor = RecordingExecutor(hang={"s1"})
    req = request(step("s0"), step("s1", timeout=1), step("s2"), parallelism=3)
    result = run(scheduler(executor), req)
    by_id = {entry.step_id: entry.status for entry in result.steps}
    assert by_id["s1"] is StepStatus.TIMED_OUT
    assert by_id["s0"] is StepStatus.SUCCESS
    assert by_id["s2"] is StepStatus.SUCCESS
    assert result.aggregate_status is BatchStatus.PARTIAL


def test_s12_batch_timeout_stops_everything() -> None:
    executor = RecordingExecutor(hang={"s0", "s1"})
    req = request(step("s0", timeout=1), step("s1", timeout=1), parallelism=2, timeout=1)
    result = run(scheduler(executor), req)
    assert result.aggregate_status is BatchStatus.TIMED_OUT
    assert all(entry.status is not StepStatus.SUCCESS for entry in result.steps)


def test_s13_no_step_starts_after_the_cancellation_decision() -> None:
    executor = RecordingExecutor(fail={"s0"})
    req = request(
        *[step(f"s{i}") for i in range(5)],
        policy=BatchFailurePolicy.FAIL_FAST,
        parallelism=1,
    )
    result = run(scheduler(executor), req)
    not_started = {
        entry.step_id for entry in result.steps if entry.status is StepStatus.NOT_STARTED
    }
    # Executor was never invoked for any step the result reports as NOT_STARTED.
    assert not_started.isdisjoint(set(executor.calls))


def test_s14_cancelled_mid_flight_step_is_indeterminate_not_compensated() -> None:
    executor = RecordingExecutor(hang={"s0"})
    req = request(step("s0", timeout=1), parallelism=1, timeout=1)
    result = run(scheduler(executor), req)
    entry = result.steps[0]
    assert entry.status in (StepStatus.CANCELLED, StepStatus.TIMED_OUT)
    if entry.error:
        assert "compensat" not in str(entry.error).lower()


def test_s15_a_failing_step_is_invoked_exactly_once() -> None:
    executor = RecordingExecutor(fail={"s0"})
    req = request(step("s0"), parallelism=1)
    run(scheduler(executor), req)
    assert executor.calls.count("s0") == 1


# --------------------------------------------------------------------------
# S-16..S-18, S-20, S-21 — per-step governance
# --------------------------------------------------------------------------


class TrackingGovernance:
    def __init__(self, *, deny: set[str] | None = None, mutations: set[str] | None = None) -> None:
        self.decisions: list[str] = []
        self.records: list[str] = []
        self._deny = deny or set()
        self._mutations = mutations or set()

    def decide(self, req: BatchRequest, item: BatchStep) -> StepDecision:
        self.decisions.append(item.step_id)
        return StepDecision(
            allowed=item.step_id not in self._deny,
            reason="POLICY_DENIED" if item.step_id in self._deny else "",
            audit_ref=f"audit:{req.batch_id}:{item.step_id}",
            idempotency_outcome=IdempotencyOutcome.EXECUTED,
            mutates=item.tool in self._mutations or item.step_id in self._mutations,
        )

    def record(self, req: BatchRequest, item: BatchStep, result) -> None:
        self.records.append(item.step_id)


def test_s16_policy_runs_once_per_step_and_a_denial_is_local() -> None:
    governance = TrackingGovernance(deny={"s1"})
    executor = RecordingExecutor()
    req = request(step("s0"), step("s1"), step("s2"))
    result = run(scheduler(executor, governance=governance), req)
    assert sorted(governance.decisions) == ["s0", "s1", "s2"]
    by_id = {entry.step_id: entry.status for entry in result.steps}
    assert by_id["s1"] is StepStatus.DENIED
    assert by_id["s0"] is StepStatus.SUCCESS
    assert "s1" not in executor.calls
    assert result.aggregate_status is BatchStatus.PARTIAL


def test_s17_approval_binds_to_the_step_digest_not_the_batch() -> None:
    a = step("a", approval_ref="apr-1")
    b = step("b", approval_ref="apr-1")
    assert a.digest() != b.digest()
    # The batch digest exists for evidence only and is not a step approval.
    req = request(a, b)
    assert req.digest() not in (a.digest(), b.digest())


def test_s18_idempotency_outcome_is_surfaced_per_step() -> None:
    governance = TrackingGovernance()
    executor = RecordingExecutor()
    result = run(
        scheduler(executor, governance=governance),
        request(step("a", idempotency_key="k-a")),
    )
    assert result.steps[0].idempotency_outcome is IdempotencyOutcome.EXECUTED


def test_s20_any_mutation_step_serialises_the_batch() -> None:
    governance = TrackingGovernance(mutations={MUTATION_TOOL})
    executor = RecordingExecutor()
    req = request(step("a"), step("b", tool=MUTATION_TOOL), parallelism=4)
    result = run(scheduler(executor, governance=governance), req)
    assert result.effective_parallelism == 1
    assert result.max_observed_inflight <= 1


def test_s20b_effective_parallelism_helper_matches_the_ceilings() -> None:
    req = request(step("a"), parallelism=BATCH_MAX_PARALLELISM)
    assert effective_parallelism(req, has_mutation=False) == BATCH_MAX_PARALLELISM
    assert effective_parallelism(req, has_mutation=True) == 1


def test_s21_every_step_result_carries_a_correlated_audit_ref() -> None:
    governance = TrackingGovernance()
    executor = RecordingExecutor()
    req = request(step("a"), step("b"), batch_id="corr-1")
    result = run(scheduler(executor, governance=governance), req)
    for entry in result.steps:
        assert entry.audit_ref == f"audit:corr-1:{entry.step_id}"
    assert sorted(governance.records) == ["a", "b"]
    assert result.evidence_ref.startswith("batch-evidence:corr-1:")


# --------------------------------------------------------------------------
# S-22, S-23, S-26, S-27 — redaction, dry run, surface, V1 invariance
# --------------------------------------------------------------------------


def test_s22_errors_carry_typed_codes_and_no_provider_payload() -> None:
    executor = RecordingExecutor(fail={"a"})
    result = run(scheduler(executor), request(step("a")))
    error = result.steps[0].error or {}
    assert error["code"] == "STEP_FAILED"
    text = str(error).lower()
    for banned in ("token", "secret", "authorization", "bearer", "password"):
        assert banned not in text


def test_s23_dry_run_executes_nothing() -> None:
    executor = RecordingExecutor()
    req = request(step("a"), step("b"), dry_run=True)
    result = run(scheduler(executor), req)
    assert executor.calls == []
    assert all(entry.status is StepStatus.NOT_STARTED for entry in result.steps)
    assert result.aggregate_status is BatchStatus.SUCCESS


def test_s26_no_prohibited_surface_in_the_phase4_modules() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "hermes_mcp_bridge" / "v2"
    banned_modules = {"subprocess", "os.system", "socket", "requests", "httpx", "urllib"}
    banned_calls = {"eval", "exec", "compile", "__import__"}
    for name in ("batch_contract.py", "batch_scheduler.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned_modules, name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned_modules, name
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in banned_calls, name


def test_s27_v1_contract_is_unchanged_with_batch_present() -> None:
    from hermes_mcp_bridge import contracts
    from hermes_mcp_bridge.v2 import batch_contract  # noqa: F401  (import has no effect)

    assert contracts.CURRENT_CONTRACT_VERSION == "1.0.0"
    assert contracts.SCHEMA_VERSION == "0.6.1"
    assert contracts.expected_tool_count() == 27


def test_batch_is_disabled_by_default() -> None:
    from hermes_mcp_bridge.v2 import batch_contract

    assert batch_contract.BATCH_FEATURE_ENABLED is False
    with pytest.raises(Exception) as excinfo:
        run(BatchScheduler(RecordingExecutor()), request(step("a")))
    assert "BATCH_DISABLED" in str(excinfo.value)
