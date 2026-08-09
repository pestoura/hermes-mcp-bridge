# Bounded Parallel Scheduling (Design)

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Depends on Phase 4 primitives that do **not** exist yet. Each dependency is
> stated as `ASSUMPTION-P4-nn` and must be satisfied by `BATCH_ACCEPTED` before
> implementation.

## Phase 4 assumptions consumed

| ID | Required Phase 4 primitive |
|---|---|
| ASSUMPTION-P4-01 | Bounded worker pool with a hard global concurrency ceiling |
| ASSUMPTION-P4-02 | Per-provider and per-credential concurrency limits |
| ASSUMPTION-P4-03 | Budget accounting (external calls, wall time, result bytes) |
| ASSUMPTION-P4-04 | Backpressure/admission rejection rather than unbounded queueing |
| ASSUMPTION-P4-05 | Circuit breaker per provider with an explicit open/half-open state |
| ASSUMPTION-P4-06 | Deterministic partial-success aggregation with per-node status |

If any assumption is unmet at implementation time, Phase 5 does not
re-implement it locally; the gap blocks the lane.

## Scheduler model

Wave-free, readiness-driven, deterministic tie-break.

```text
ready(n)  := every p in n.depends_on has status SUCCESS
             and no p is SKIPPED/FAILED under fail_fast
             and every binding source value is present and validated
dispatch  := while capacity available and ready set non-empty:
                pick next by (topological_rank, node_id)  # deterministic
                acquire global slot, provider slot, credential slot
                dispatch
```

- `topological_rank` is the Kahn layer index computed during validation and is
  part of the plan's canonical form, so ordering is reproducible across runs
  and across restarts after resume.
- Determinism of *dispatch order* does not imply determinism of *completion
  order*; aggregation is order-independent (see `failure-semantics.md`).
- `max_parallelism` is `min(plan.budget.max_parallelism, engine ceiling,
  provider limit, credential limit)`. The smallest bound always wins.

## Slot acquisition ordering

Always global → provider → credential, released in reverse. Fixed ordering
avoids deadlock between plans competing for the same credential class. A node
that cannot acquire all slots within `admission_wait_ms` does not spin: it
returns to the ready set and the scheduler proceeds with other ready nodes,
recording a `SCHED_DEFERRED` counter. Repeated deferral past the plan deadline
yields `DEADLINE_EXCEEDED`, not starvation.

## Mutating-node serialization

Nodes classified mutating against the **same resource** are never dispatched
concurrently, even if the graph permits it. The scheduler derives a resource
lock key from the Phase 3 operation scope (e.g. `github:owner/repo:branch`) and
serializes on it. Concurrency between distinct resources is allowed.
Rationale: preserves the Phase 3 optimistic-concurrency proofs (A3-08) under a
parallel scheduler.

## Cancellation and deadlines

- Plan deadline is absolute and computed at admission, not per node.
- Node `timeout_ms` is clamped to remaining plan budget.
- Cancellation is cooperative at dispatch boundaries plus hard timeout at the
  provider client layer. An in-flight mutation that cannot be proven aborted
  yields `INDETERMINATE` (see `failure-semantics.md`), never `FAILED`.
- Cancellation never fabricates a compensation for an `INDETERMINATE` node.

## Circuit breaker interaction

An open breaker for a provider makes all of that provider's ready nodes
non-dispatchable. Under `fail_fast` the plan aborts with
`PROVIDER_UNAVAILABLE`; under `continue_independent` only the affected subgraph
is `SKIPPED` and independent branches complete. A breaker-blocked node is never
counted as a failure of the node's own logic.

## Retry

Retries are per node, bounded, from a named retry class (OD-011 open), and only
for operations classified retry-safe. A mutating node is retried only when its
idempotency key guarantees at-most-once commitment at the provider — otherwise
a transport failure is `INDETERMINATE`, not a retry candidate. Retries consume
budget and never extend the plan deadline.

## What the scheduler must not do

- No dynamic node creation. The graph is fixed at digest time.
- No re-ordering that violates declared dependencies for "efficiency".
- No speculative execution of a node whose upstream is unresolved.
- No implicit parallelism widening when a provider is fast.
- No cross-plan work stealing that bypasses per-credential limits.

## Requirements traced

V2-FR-002, V2-FR-003, V2-FR-010, V2-FR-011, V2-NFR-002, ADR-0008, ADR-0009.
