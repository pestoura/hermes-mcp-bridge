# Failure Propagation and `INDETERMINATE` Behaviour (Design)

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

## Node terminal statuses

| Status | Meaning | Effects committed? |
|---|---|---|
| `SUCCESS` | Executed, result shaped and validated | Yes (if mutating) |
| `FAILED` | Executed and **provably** did not commit an effect | No |
| `DENIED` | Policy/scope/approval/readiness refusal before execution | No, zero credential resolution, zero HTTP |
| `SKIPPED` | Not executed because an upstream did not succeed, or a breaker blocked it | No |
| `INDETERMINATE` | Outcome unknown; commitment can neither be confirmed nor excluded | Unknown |
| `DEAD_LETTER` | Requires human action | Unknown or unrecoverable |

`FAILED` is a strong claim. It may only be used when the failure is provably
pre-commit: validation rejection, policy denial, connection refused before the
request was sent, provider 4xx that is defined as non-committing (e.g. 422
validation), or a read-back proving absence. Anything else is `INDETERMINATE`.

## Plan terminal statuses

| Status | Condition |
|---|---|
| `COMPLETED` | Every node `SUCCESS` |
| `PARTIAL` | At least one `SUCCESS` and at least one non-success, no `INDETERMINATE` |
| `FAILED` | No committed effects remain (either none occurred or all were verifiably compensated) |
| `ABORTED` | Caller cancellation or deadline, handled per `rollback_policy` |
| `INDETERMINATE` | **Any** node is `INDETERMINATE` and unresolved |
| `DEAD_LETTER` | Manual intervention required |

Precedence: `DEAD_LETTER` > `INDETERMINATE` > `ABORTED` > `PARTIAL` > `FAILED`
> `COMPLETED`. A single `INDETERMINATE` node makes the whole plan
`INDETERMINATE`; it cannot be reported as `PARTIAL`, because `PARTIAL` implies a
known effect set.

## Propagation

Under `failure_policy: fail_fast`:

- the first non-success terminal node stops new dispatch;
- in-flight nodes are allowed to reach a terminal state (cancelled where safe);
- all unstarted nodes become `SKIPPED` with reason `UPSTREAM_ABORT`;
- `rollback_policy` then applies.

Under `failure_policy: continue_independent`:

- only the transitive descendants of the failed node become `SKIPPED` with
  reason `UPSTREAM_FAILED`;
- nodes with no dependency path from the failure continue;
- the plan reports `PARTIAL` with a complete per-node status map.

`on_failure` per node refines this: `abort_plan` forces fail-fast behaviour for
that node even under `continue_independent`; `isolate_branch` does the reverse;
`dead_letter` routes the node (and the plan) to manual intervention immediately.

Descendant skipping is computed over the declared edge set, which is why
undeclared binding edges are a validation error — an undeclared edge would let
a dependent run on stale or missing data.

## `INDETERMINATE` behaviour (normative)

An `INDETERMINATE` node:

1. is **never** retried automatically;
2. is **never** compensated automatically;
3. **blocks** all its dependents, which become `SKIPPED` with reason
   `UPSTREAM_INDETERMINATE` (never executed on a guess);
4. forces a durable checkpoint write before any recovery attempt;
5. triggers **read-only** reconciliation (see `checkpoint-and-resume.md`),
   bounded by a reconciliation window and a bounded attempt count;
6. resolves to `SUCCESS` or `PENDING` only on positive, read-verified evidence;
7. on window expiry, becomes `DEAD_LETTER` with an operator report enumerating
   the idempotency key, the expected effect shape and the last observed state;
8. never yields a plan status better than `INDETERMINATE`.

Entry conditions include: request sent with no response, transport reset after
send, ambiguous 5xx on a mutating call, timeout after dispatch, lease loss while
a mutating node was `DISPATCHED`, and provider rate-limit disconnect on a write.

Prohibited "recoveries": assuming failure because the response was lost,
assuming success because the operation is "usually fast", re-issuing a mutation
without an idempotency guarantee, or compensating a possibly-nonexistent effect.

## Reporting contract

Every terminal plan response contains:

```text
status                 plan terminal status
node_statuses          {node_id: status + reason_code}
committed_effects      [ {node_id, effect_ref} ]     explicit, never omitted
compensated_effects    [ {node_id, effect_ref, outcome} ]
unknown_effects        [ {node_id, idempotency_key, expected_shape} ]
budget_consumed        counters
plan_digest            identity
execution_id           for resume/reconciliation
```

`unknown_effects` non-empty ⇒ plan status is `INDETERMINATE` or `DEAD_LETTER`.
A response may never be silent about a possible side effect. This is gate
criterion A5-12.

## Requirements traced

V2-FR-010, V2-FR-011, V2-FR-024, V2-SEC-004, V2-SEC-011, ADR-0008, ADR-0009,
ADR-0014.
