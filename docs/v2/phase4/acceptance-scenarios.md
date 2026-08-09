# BATCH Acceptance Scenarios (`BATCH_ACCEPTED`)

> **V2 · PHASE 4 · DESIGN ONLY · NOT_IMPLEMENTED · DO_NOT_MERGE UNTIL DIRECT_MUTATION_ACCEPTED**

All scenarios run **offline** with an injected fake step executor and injected
clock. No provider calls. `BATCH_ACCEPTED` requires every scenario to pass with
`failures=[]`; any unimplemented scenario is a failure, not a skip.

| ID | Scenario | Expected |
|---|---|---|
| S-01 | **Non-serial execution.** 4 independent read steps, `max_parallelism=2`, executor blocks on a barrier releasing at 2 concurrent entrants | Barrier releases; `max_observed_inflight == 2`; overlapping intervals; a serial implementation deadlocks and fails |
| S-02 | Parallelism ceiling honoured: `max_parallelism=2`, 6 steps | `max_observed_inflight` never exceeds 2 at any sampled instant |
| S-03 | `max_items` exceeded (`BATCH_MAX_ITEMS + 1` steps) | `DENIED` at validation, zero executor invocations, evidence with `steps: []` |
| S-04 | Caller requests `max_parallelism` above ceiling | `DENIED`; never silently clamped |
| S-05 | Result completeness: any terminal outcome | `len(result.steps) == len(request.steps)` and `sum(counts.values()) == len(steps)` |
| S-06 | Result ordering: completion order shuffled by the fake executor | `result.steps` is in request order |
| S-07 | Backpressure: live task count sampled during execution | Never exceeds `effective_parallelism`; no unbounded task spawning |
| S-08 | Global capacity: batches beyond `BATCH_MAX_INFLIGHT_GLOBAL` inflight steps | `BATCH_CAPACITY_EXHAUSTED` at admission, not unbounded queueing |
| S-09 | `continue_on_error`: step 2 of 4 fails | Steps 1,3,4 `SUCCESS`; aggregate `PARTIAL` |
| S-10 | `fail_fast`: step 2 of 4 fails while 3 is in flight | Admission closed before cancellation; unstarted step 4 `NOT_STARTED`; in-flight step 3 `CANCELLED`; aggregate `PARTIAL` |
| S-11 | Step timeout | That step `TIMED_OUT`, slot released, others unaffected under `continue_on_error` |
| S-12 | Batch timeout | Aggregate `TIMED_OUT`; no step starts after the deadline |
| S-13 | Fail-closed cancellation ordering | No step transitions from `NOT_STARTED` to started after the cancellation decision (asserted via executor call log) |
| S-14 | Cancelled mid-mutation | Step `CANCELLED`, audit marks outcome indeterminate, no compensating call issued |
| S-15 | No retries | A failing step is invoked exactly once by the executor |
| S-16 | Per-step policy | Policy engine invoked once per step; a `DENIED` step does not deny its siblings under `continue_on_error` |
| S-17 | Approval binding | Approval bound to step A rejected when presented for step B → step `DENIED` |
| S-18 | Idempotency replay | Identical batch replayed → previously committed steps report `REPLAYED`, no duplicate side effects |
| S-19 | `batch_id` reuse with different digest | `DENIED` at validation |
| S-20 | Mutation serialisation | Any mutation step present → `effective_parallelism == 1` |
| S-21 | Audit correlation | Every step audit record carries `batch_id` + `step_id`; envelope record lists all `audit_ref`s |
| S-22 | Evidence redaction | No token, header, credential or provider payload in the evidence record; only typed error codes |
| S-23 | Dry run | Zero executor invocations, zero idempotency writes, all steps `NOT_STARTED` or `DENIED` |
| S-24 | Unknown envelope field / unknown `tool` / duplicate `step_id` | `DENIED`, nothing executed |
| S-25 | Non-empty `depends_on` | `DENIED` (reserved for DAG mode) |
| S-26 | No prohibited surface | Static scan finds no shell/subprocess/generic-HTTP symbol in the Phase 4 module set |
| S-27 | V1 invariance | Tool count 27, contract `1.0.0`, schema `0.6.1` unchanged with BATCH present but disabled |

## Gate

`BATCH_ACCEPTED` is declared only when S-01..S-27 pass with `failures=[]`, the
Phase 4 preflight scan passes, and the full existing suite is green. Until then
BATCH remains disabled by default behind an explicit feature flag defaulting to
off.
