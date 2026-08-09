# Per-Node Governance: Policy, Idempotency, Audit Inheritance (Design)

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

Core rule, unchanged from ADR-0011: **one request is not one authorization
decision.** A DAG is a container, not a privilege.

## Policy inheritance

| Aspect | Rule |
|---|---|
| Evaluation unit | Every node, independently, using the Phase 1 fail-closed engine |
| Plan-level policy | May only **narrow**. There is no plan-level ALLOW that grants a node anything |
| Absent policy | DENY (no default allow), reason `POLICY_MISSING` |
| Decision timing | At validation (step 10) and re-checked at dispatch if the node was queued past `policy_revalidate_ms` |
| Resume | Re-evaluated (see `checkpoint-and-resume.md` step 4) |
| Bound arguments | Policy input includes the **resolved** argument values, not just literals; a value produced by a binding is subject to the same rules |
| Decision record | Node-level decision, reason code and policy digest persisted in the checkpoint and audit record |

`APPROVAL_REQUIRED` on any node makes the plan approval-gated: the plan cannot
start any node until the approval bound to `plan_digest` is consumed. Partial
approval of a subset of nodes is not supported in Phase 5 — it creates a
time-of-check window across the graph.

## Credential inheritance

- A node uses exactly the credential capability id declared by its registry
  tool. A plan cannot select, override or widen a credential.
- Readiness is checked per node before dispatch, not once per plan, because
  readiness can change mid-execution (rotation, revocation).
- A denied node performs **zero** credential resolution (Phase 2 property,
  preserved).
- Mixing `github.read` and `github.write` nodes in one plan is allowed; the
  write capability is never used to satisfy a read node and vice versa
  (Phase 3 A3-03 preserved per node).
- Credential material never enters `args`, `bindings`, results, checkpoints,
  transforms, audit records or error bodies.

## Idempotency inheritance

Every mutating node carries a key derived deterministically:

```text
node_idempotency_key = H( plan_digest
                        || node_id
                        || attempt_epoch
                        || canonical(resolved_args) )
```

- `plan_digest` scoping means the same plan re-submitted with the same digest
  reuses keys (true idempotency), while a modified plan gets fresh keys.
- `attempt_epoch` is bumped only by an explicit operator-authorized
  force-retry, never by automatic retry — automatic retries must reuse the key.
- `canonical(resolved_args)` includes binding-produced values, so a different
  upstream result is correctly a different operation.
- Keys are written to the store **before** dispatch (write-ahead) and are the
  reconciliation handle after a crash.
- Key material is a digest; it is not secret but is never used as an
  authorization token.

Phase 3's `operation_digest` remains the per-mutation approval artifact; the
node key composes over `plan_digest` so the two are consistent rather than
competing.

## Audit inheritance

Every node emits its own audit record; a plan emits an enveloping record.

```text
PlanAuditRecord   : execution_id, plan_digest, principal_ref, projection_digest,
                    policy_digest, approval_ref, budget, admission decision,
                    terminal status, node status histogram, replay flag
NodeAuditRecord   : execution_id, node_id, tool/op, security tier,
                    policy decision + reason, credential capability id (never
                    material), idempotency key, attempt, timings, provider
                    status class, effect_ref, result digest (not result body),
                    redacted error reason, compensation linkage
```

Properties:

- Write-ahead for mutating nodes (inherited from Phase 3 A3-05): no committed
  mutation may exist without a preceding audit record.
- Records are append-only and linked by `execution_id` + `plan_digest`, giving
  a complete causal chain across resume and compensation.
- Redaction scan applies to every field (Phase 3 A3-14 preserved).
- Free-text editorial metadata is excluded from canonical/audit serialization
  (Phase 1 property preserved).

## Observability

Per-node metrics inherit the node's tool labels. Cardinality control: labels are
tool id, security tier, terminal status class and provider — **never** node id,
plan id, repository name or any caller-controlled string.

Token accounting: the DAG path consumes **zero** Hermes LLM tokens. This is a
measured criterion (A5-13), not an assertion.

## Requirements traced

V2-SEC-003, V2-SEC-005, V2-SEC-011, V2-SEC-019, V2-FR-018, V2-FR-020,
ADR-0011, ADR-0012, ADR-0013, ADR-0018.
