# BATCH Concurrency, Scheduling and Ordering

> **V2 · PHASE 4 · DESIGN · unblocked by `DIRECT_MUTATION_ACCEPTED` (a86b26d) · runtime gated behind `BATCH_FEATURE_ENABLED` until `BATCH_ACCEPTED`**

## Execution model

Steps in a Phase 4 batch are **independent by construction** (`depends_on` must
be empty). The scheduler therefore runs them with bounded concurrency:

- A single asyncio scheduler owns the batch.
- Admission is guarded by a semaphore of size
  `effective_parallelism = min(request.max_parallelism, BATCH_MAX_PARALLELISM,
  BATCH_MAX_PARALLELISM_MUTATION if any step mutates else BATCH_MAX_PARALLELISM)`.
- Each admitted step runs the **unchanged DIRECT execution path**. The scheduler
  never reimplements provider calls, policy or credential resolution.
- No thread pool, no subprocess, no forked worker: concurrency is cooperative
  I/O concurrency only.

## Non-serial execution is required, not optional

With `effective_parallelism >= 2` and independent steps, execution **must**
overlap. This is an acceptance requirement, not an optimisation:

- Observable property: for two independent steps A and B with
  `effective_parallelism = 2`, the intervals
  `[A.started_at, A.finished_at]` and `[B.started_at, B.finished_at]` overlap
  when both are non-trivially long.
- Test strategy: a deterministic fake step executor gated on a barrier that only
  releases when N steps are simultaneously in flight. A serial implementation
  deadlocks the barrier and the test fails on timeout. See `acceptance-scenarios.md`
  S-01.

## Ordering semantics

1. **Admission order** is the request order of `steps`. The scheduler starts
   steps in list order as slots free up; it does not reorder or prioritise.
2. **Completion order is not defined.** Callers must not depend on it.
3. **Result order is defined**: `BatchResult.steps` is always in the request
   order, regardless of completion order. Result ordering is a presentation
   guarantee, never an execution guarantee.
4. **No happens-before between steps.** A batch is not a transaction and gives
   no cross-step consistency. Callers needing ordering must issue separate
   requests, or wait for the DAG mode.

## Mutation concurrency

When any step targets a mutation tool, effective parallelism is forced to `1`
in Phase 4. Rationale: Phase 3 idempotency and optimistic-concurrency semantics
were accepted for single in-flight mutations; raising mutation concurrency is a
separate, evidence-backed decision after `BATCH_ACCEPTED`.

Mixed batches (reads + mutations) are therefore fully serialised in Phase 4.
Read-only batches get the full concurrency ceiling.

## Fairness

Within a process, the global semaphore is FIFO to avoid starving a batch that
arrived earlier. No priority classes in Phase 4.

## Determinism for tests

The scheduler must expose an injectable clock and an injectable step executor so
that concurrency, timeout and cancellation behaviour is testable offline with
no provider calls and no wall-clock sleeps beyond short barriers.
