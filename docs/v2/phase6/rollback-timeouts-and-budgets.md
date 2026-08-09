# Rollback Support, Timeouts, Budgets and Cancellation

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Refines `../adrs/ADR-0014-saga-compensation.md` and the Phase 3
> `../phase3/rollback-and-compensation.md` table for multi-node runbooks.

## `rollback_support`

Mandatory enum on the runbook and on every mutating node:

| Value | Meaning |
|---|---|
| `NOT_APPLICABLE` | Node performs no external mutation |
| `AUTOMATIC` | A declared compensation exists, is itself a registered governed mutation, and is proven by test |
| `MANUAL` | A documented human procedure exists; the engine will not attempt it |
| `NOT_SUPPORTED` | No rollback exists; requires `accepted_irreversibility` (see `policy-approval-and-destructive-actions.md`) |

Rules:

1. Compensation is a **declaration**, never an assumption (V2-SEC-021). An
   undeclared compensation is `NOT_SUPPORTED`, not "probably fine".
2. Each compensation is an independently governed mutation: it has its own
   capability requirement, its own policy evaluation, its own audit record and
   its own idempotency key. It does not inherit a bypass from the forward
   operation.
3. The runbook's aggregate `rollback_support` is the **weakest** value across
   its mutating nodes. A runbook with one `NOT_SUPPORTED` node is
   `NOT_SUPPORTED`.
4. Compensation runs in reverse topological order over the nodes that actually
   committed, determined from the write-ahead audit records, not from the
   planned graph.
5. If compensation cannot be proven safe — unknown committed state, drifted
   resource, unreadable protection state, expired credential — the execution
   dead-letters to `MANUAL_INTERVENTION_REQUIRED` **without attempting a
   write** (inherits Phase 3 A3-12).
6. Partial compensation is reported explicitly as `COMPENSATED_PARTIAL` with
   the exact residual object list; it is never reported as `COMPENSATED`.

## Timeouts

| Field | Scope | Rule |
|---|---|---|
| `timeout_ms` (runbook) | whole execution | Mandatory, bounded by a registry maximum; missing → `RB_TIMEOUT_MISSING` |
| `node_timeout_ms` | each node | Mandatory per node; the sum of the critical path must not exceed `timeout_ms`, checked at admission (`RB_TIMEOUT_INCONSISTENT`) |
| `approval_ttl_ms` | approval validity | Mandatory when `approval_class ≠ NONE`; short by default |
| `lease_ttl_ms` | execution lease | Mandatory; heartbeat renewal required (inherits Phase 5 lease model) |

Deadlines propagate: a node never receives a deadline later than the remaining
runbook deadline. Timeout is a terminal state (`TIMED_OUT`), and for mutating
runbooks it triggers the compensation path, not a silent retry.

## Retry classes

| Class | Applies to |
|---|---|
| `NO_RETRY` | Destructive nodes, governed merge, any node without a proven idempotency key |
| `IDEMPOTENT_RETRY` | Nodes with a provider-verified idempotency key; bounded attempts, jittered backoff |
| `SAFE_READ_RETRY` | Pure reads |

Rate-limit / `Retry-After` handling must never produce a duplicate write
attempt (inherits Phase 3 A3-15). Retry defaults remain OD-011; until resolved,
admission requires an explicit per-node retry class and rejects reliance on a
default (`RB_RETRY_CLASS_MISSING`).

## Budgets

The runbook declares defaults, and the caller may only tighten them, never
widen:

`max_nodes`, `max_external_calls`, `max_parallelism`, `max_runtime_ms`,
`max_result_bytes`, `max_artifacts`, `max_retries`,
`max_agentic_escalations` (default **0**), `max_agentic_tokens` (default **0**).

A runbook is deterministic by default: `max_agentic_escalations = 0`. Any
non-zero value requires `policy_class ≥ MUTATING_LOW` review, an explicit
escalation reason-code allow-list (Phase 8 / OD-020) and is rejected until
`HYBRID_ACCEPTED` exists (`RB_AGENTIC_NOT_PERMITTED`).

Budget exhaustion is a terminal state with an explicit reason code, not a
truncated success.

## Cancellation

Cancellation propagates to running nodes and to the scheduler (V2-FR-019). A
cancelled mutating runbook enters the same compensation evaluation as a
failure. A yank of the runbook version during execution triggers cancellation
at the next node boundary; nodes already committed are compensated or
dead-lettered — never left unrecorded.

## Terminal states

`SUCCESS`, `FAILED`, `PARTIAL_SUCCESS`, `CANCELLED`, `TIMED_OUT`,
`COMPENSATED`, `COMPENSATED_PARTIAL`, `MANUAL_INTERVENTION_REQUIRED`.

`continue_on_error` is not selectable for mutating nodes; caller preference
cannot loosen operation semantics.
