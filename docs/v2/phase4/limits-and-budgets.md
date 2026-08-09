# BATCH Limits, Budgets and Timeouts

> **V2 · PHASE 4 · DESIGN · unblocked by `DIRECT_MUTATION_ACCEPTED` (a86b26d) · runtime gated behind `BATCH_FEATURE_ENABLED` until `BATCH_ACCEPTED`**

## Server-side ceilings (authoritative)

| Constant | Proposed value | Meaning |
|---|---|---|
| `BATCH_MAX_ITEMS` | `10` | Hard maximum steps per batch |
| `BATCH_MAX_PARALLELISM` | `4` | Hard maximum concurrent steps per batch |
| `BATCH_MAX_PARALLELISM_MUTATION` | `1` | Concurrency ceiling when any step is a mutation tool |
| `BATCH_MAX_TIMEOUT_S` | `300` | Hard maximum `batch_timeout_s` |
| `BATCH_MAX_INFLIGHT_GLOBAL` | `8` | Process-wide concurrent batch steps across all batches |

The caller may only request values **at or below** the ceilings. A caller value
above a ceiling is a validation error (`DENIED`, batch not started) — it is
never silently clamped, because silent clamping hides intent from the audit
record.

## Rejection is pre-execution and total

Budget violations are detected during validation, before any step runs:

- `len(steps) > BATCH_MAX_ITEMS` → whole batch `DENIED`, zero side effects.
- `max_parallelism > ceiling` → whole batch `DENIED`.
- `batch_timeout_s > BATCH_MAX_TIMEOUT_S` → whole batch `DENIED`.
- any `step_timeout_s > batch_timeout_s` → whole batch `DENIED`.
- duplicate `step_id` → whole batch `DENIED`.
- unresolvable `tool` → whole batch `DENIED`, including steps that would resolve.

Fail-closed: an ambiguous or unparsable envelope is `DENIED`, never partially
executed.

## Timeout hierarchy

1. **Step timeout** — `step_timeout_s` bounds a single step. Expiry marks that
   step `TIMED_OUT` and releases its concurrency slot. Under
   `fail_fast` it also triggers batch cancellation.
2. **Batch timeout** — `batch_timeout_s` bounds the whole wall clock. On expiry
   the scheduler stops admitting new steps, cancels in-flight steps
   (see `failure-and-cancellation.md`) and returns `TIMED_OUT`.
3. **Transport/provider timeout** — unchanged from DIRECT; the step timeout must
   be greater than or equal to the underlying DIRECT timeout so the batch layer
   never masks a provider-level classification.

A timeout is a terminal outcome, not a retry trigger. Phase 4 performs **no
automatic retries** (see `non-goals.md`).

## Backpressure

- Admission control is a semaphore of size `min(request.max_parallelism,
  ceiling)` per batch, plus a global semaphore `BATCH_MAX_INFLIGHT_GLOBAL`.
- When the global semaphore is saturated, new *batches* are rejected at
  admission with a typed `BATCH_CAPACITY_EXHAUSTED` error rather than queued
  unboundedly. Bounded queueing with a deadline may be added only if the
  deadline is strictly inside `batch_timeout_s`.
- No unbounded internal queue, no unbounded task spawning: the number of live
  tasks is bounded by the semaphores at all times. This is a testable
  invariant (`acceptance-scenarios.md`, S-07).
- Per-provider limits from Phase 3 still apply underneath and can further
  restrict effective concurrency; the batch layer must not bypass them.

## Quota and cost accounting

Each step consumes its own quota/budget exactly as in DIRECT. The batch
envelope adds one aggregate counter (`external_calls_total`) used for evidence
and metrics only — it is never a substitute for per-step quota enforcement.
