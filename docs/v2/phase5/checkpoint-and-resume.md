# Persisted Checkpoint / Resume Contract (Design)

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Store technology is **OD-003 (open)**. This document specifies the contract a
> store must satisfy, not the implementation.

## Purpose

A DAG execution can outlive a process. Resume must be safe: never duplicate a
mutation, never widen scope, never resurrect an approval, never trust a
checkpoint that does not belong to the exact plan.

## Checkpoint record

```text
Checkpoint
  execution_id        : uuid, server-generated
  plan_digest         : canonical digest, immutable
  schema_version      : engine state schema version, fail-closed on mismatch
  principal_ref       : opaque caller identity reference (OD-007 open)
  projection_digest   : digest of the capability projection at admission
  policy_digest       : digest of the evaluated policy decision set
  approval_ref        : consumed approval id + nonce, or null
  created_at / updated_at
  node_states         : {node_id: NodeState}
  budget_consumed     : counters
  status              : RUNNING | PAUSED | COMPLETED | FAILED | PARTIAL | ABORTED | DEAD_LETTER
  lease               : Lease
  record_digest       : integrity digest over the above
```

```text
NodeState
  status         : PENDING | READY | DISPATCHED | SUCCESS | FAILED | SKIPPED
                 | INDETERMINATE | COMPENSATED | COMPENSATION_FAILED | DEAD_LETTER
  attempt        : int
  idempotency_key: str        (present for mutating nodes)
  effect_ref     : provider-side identifier of the committed effect, or null
  result_ref     : artifact/store reference to the shaped result, or null
  started_at / ended_at
  reason_code    : stable code, redacted message
```

Checkpoints store **shaped, allow-listed results only** — never raw provider
payloads, never credential material, never headers. Size bounded by
`budget.max_checkpoint_bytes`; exceeding it fails the node rather than
truncating silently (truncation would corrupt bindings).

## Write points

A checkpoint write is required, and must be durable before proceeding, at:

1. admission (plan accepted, digest computed, approval consumed);
2. **before** dispatching any mutating node (write-ahead, mirroring Phase 3
   A3-05) including the idempotency key;
3. after each node reaches a terminal state;
4. on transition to `INDETERMINATE`, before any recovery attempt;
5. before and after each compensation step;
6. at plan terminal status.

Write-ahead ordering is the invariant that makes resume safe: if the process
dies between (2) and the provider call, resume sees a `DISPATCHED` mutating node
with a known idempotency key and treats it as `INDETERMINATE` until reconciled.

## Lease and heartbeat

```text
Lease
  owner_id     : engine instance id
  acquired_at
  expires_at
  heartbeat_at
  fence_token  : monotonically increasing integer
```

- Only the lease owner may write node states. Writes carry `fence_token`; a
  stale token is rejected by the store (compare-and-set).
- Heartbeat interval ≤ lease TTL / 3.
- Lease expiry does not cancel a plan; it makes it **recoverable**.
- A recovering instance must acquire the lease with a strictly greater
  `fence_token` before any read-modify-write. Two instances can never both
  dispatch the same node.

## Resume algorithm

```text
1  load checkpoint; verify record_digest; mismatch -> DEAD_LETTER (tamper)
2  verify schema_version supported; else PAUSED, manual intervention
3  recompute plan_digest from the stored PlanDefinition; mismatch -> DEAD_LETTER
4  re-verify projection_digest and policy_digest against current state
      any drift -> re-evaluate policy per node; a now-DENY node is SKIPPED,
      never executed under the old decision
5  approvals are NOT re-consumed and NOT re-issued; an expired approval
      blocks its nodes (PAUSED / manual intervention)
6  reconcile every DISPATCHED / INDETERMINATE node (below)
7  rebuild the ready set from node_states; resume scheduling
```

Step 4 is deliberate: a resume is a new authorization moment. Policy that
tightened between runs must take effect. Policy that loosened does **not**
retroactively enable a node that was denied and skipped — that requires a new
plan.

## Reconciliation of `DISPATCHED` / `INDETERMINATE`

For a mutating node, reconciliation is a **read-only** provider query keyed by
the idempotency key or by the deterministic expected effect (e.g. branch ref
existence, PR with the recorded head/base and digest marker):

| Reconciliation outcome | New state |
|---|---|
| Effect present and matches the expected shape | `SUCCESS`, `effect_ref` recorded |
| Effect provably absent and provider is authoritative | `PENDING` (safe to re-dispatch under the same idempotency key) |
| Provider unreachable, ambiguous, or shape mismatch | remains `INDETERMINATE` |

An `INDETERMINATE` node is never re-dispatched and never compensated
automatically. It blocks its dependents and routes the plan to
manual intervention. See `failure-semantics.md`.

For a read-only node, reconciliation is unnecessary: it is simply re-executed.

## Dead-letter and manual intervention

`DEAD_LETTER` is terminal for the execution and requires human action. Entry
conditions: checkpoint integrity failure, plan digest mismatch, unsupported
state schema, compensation failure, or an `INDETERMINATE` node whose resolution
window expired. The dead-letter record retains the redacted node states, the
digests and the reason code — never secret material.

## Replay simulation

Replay (V2-FR-019, OD-021 open) re-executes a plan against recorded shaped node
outputs with all providers disabled. Replay must be observably distinct from
execution: separate execution id, `replay=true` in every audit record, zero
credential resolution, zero external calls, and it can never consume an
approval or write an idempotency key.

## Requirements traced

V2-FR-009, V2-FR-024, V2-FR-019, V2-SEC-011, V2-SEC-019, ADR-0009, ADR-0013.
Open: OD-003 (store), OD-018 (canonical serialization), OD-021 (replay format).
