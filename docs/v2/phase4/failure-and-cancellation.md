# BATCH Failure, Cancellation and Partial Results

> **V2 · PHASE 4 · DESIGN · unblocked by `DIRECT_MUTATION_ACCEPTED` (a86b26d) · runtime gated behind `BATCH_FEATURE_ENABLED` until `BATCH_ACCEPTED`**

## Failure policies

`failure_policy` is mandatory and explicit.

### `continue_on_error`

- A failing step does not stop other steps.
- Every step reaches a terminal status.
- Aggregate is `SUCCESS` only if all steps are `SUCCESS`; otherwise `PARTIAL`
  (at least one `SUCCESS`) or `FAILED`/`DENIED` (none).

### `fail_fast`

- The first terminal non-`SUCCESS` step triggers batch cancellation.
- In-flight steps are cancelled; unstarted steps are `NOT_STARTED`.
- Aggregate is `PARTIAL` when some steps already succeeded, otherwise `FAILED`.

## Fail-closed cancellation

Cancellation is **fail-closed**: when the batch is cancelled (fail-fast trigger,
batch timeout, capacity loss, or caller/transport disconnect):

1. The admission semaphore is closed first — **no new step may start** after the
   cancellation decision. This ordering is normative: close admission, then
   cancel in flight.
2. In-flight steps are cancelled at the first safe await point. A step that has
   already issued a mutating provider call is **not** assumed to have failed: it
   is recorded as `CANCELLED` with `idempotency_outcome` left as reported, and
   the audit record marks the outcome as *indeterminate at the bridge*.
3. Never-started steps are `NOT_STARTED` with `result=None` and no audit record
   beyond "not attempted".
4. Ambiguity always resolves to *do less*: if the scheduler cannot prove a step
   is safe to start, it does not start it.

## No compensation, no rollback

Phase 4 performs **no compensating actions** and **no rollback** for already
completed steps. A batch is not a transaction. Successful side effects stay.
Compensation remains the Phase 3 `rollback-and-compensation.md` decision for
single mutations and is out of scope here.

## No automatic retries

A failed, timed-out or cancelled step is never retried by the batch layer. Retry
is the caller's decision, and replaying the same batch is safe only because each
step carries its own `idempotency_key` (see `step-governance.md`).

## Error typing

Step errors reuse the DIRECT typed error taxonomy. The batch layer adds only:

| Code | Meaning |
|---|---|
| `BATCH_VALIDATION_FAILED` | Envelope/budget violation; nothing executed |
| `BATCH_CAPACITY_EXHAUSTED` | Global inflight ceiling reached at admission |
| `BATCH_TIMEOUT` | Batch wall clock exceeded |
| `BATCH_CANCELLED` | Cancelled by fail-fast or caller disconnect |

No provider payloads, tokens, headers or credentials appear in errors; the
Phase 3 redaction path is reused unchanged.
