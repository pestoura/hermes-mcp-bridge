# Capability and Credential Requirements

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Refines `../adrs/ADR-0007-least-privilege-credentials.md` and
> `../adrs/ADR-0006-credential-broker.md` for the runbook case. See ADR-0030.

## Declaration

A runbook declares, in the IR:

| Field | Meaning |
|---|---|
| `requires_capabilities[]` | Capability IDs the runbook needs, e.g. `github.read`, `github.write.branch` |
| `requires_tools[]` | `(tool_name, tool_version)` pins for every node |
| `credential_capability_ids[]` | Broker capability IDs, status-only; never material |
| `resource_scope` | Declared scope expression (repositories, entities) |
| `min_capability_state` | Required readiness state per capability (default `READY`) |

## Least privilege

1. The runbook's capability set is the **union of what its nodes actually
   need**, computed at admission from the node tool references — not what the
   author typed. If the declared set is a superset of the computed set,
   admission fails with `RB_CAPABILITY_SUPERSET`. A subset fails with
   `RB_CAPABILITY_MISSING`. Exact match only.
2. Read and write capabilities remain separate (Phase 3 `credential-split.md`).
   A runbook that only reads may not declare a write capability, even "for
   later".
3. Administrative capabilities (`github.admin`, repository deletion, permission
   changes) are excluded **by capability**, not by policy: no registry entry
   exists, so no runbook can reference them. A manifest referencing one is
   rejected with `RB_ADMIN_CAPABILITY_FORBIDDEN` (inherits ADR-0023).
4. Credential material is never in the manifest, the IR, the parameters, the
   result, the logs, the traces, the metric labels or the evidence. The broker
   returns status only; the executor holds material for the shortest possible
   window and never serializes it (V2-SEC-025).

## Effective authority at invocation

The authority used for an execution is the intersection of:

```
effective = runbook.requires_capabilities
          ∩ caller.authorized_capabilities
          ∩ policy.allowed_capabilities(context)
          ∩ broker.ready_capabilities
```

If the intersection is not equal to `runbook.requires_capabilities`, the
invocation is denied with `RB_INSUFFICIENT_CAPABILITY` **before** any node
runs. A runbook never executes partially because only some capabilities were
available; there is no "start and see how far we get" mode for mutating
runbooks.

Runbook metadata must not expand caller authority (V2-SEC-013): declaring a
capability grants nothing. Authority comes only from the caller's principal and
policy.

## Readiness before resolution

Ordering is fixed and fail-closed, matching the Phase 2 read path:

1. schema validation
2. scope check
3. policy evaluation
4. capability readiness probe (`min_capability_state`)
5. approval consumption (if required)
6. credential resolution
7. provider call

A failure at any step stops the sequence; later steps must be observably not
executed (zero broker calls, zero HTTP requests) and the tests must assert that.

## Per-node projection

Each node receives only the credentials its own tool requires, for the duration
of that node. A runbook does not hold a merged super-credential across nodes.
Node-level credential scope is recorded in the audit record by capability ID.

## Capability drift

The `capability_snapshot_hash` in force at approval time is bound into
`plan_digest` (see `plan-digest-binding.md`). If the snapshot changes between
approval and execution, the approval is invalid and the execution is denied
with `RB_CAPABILITY_DRIFT` rather than proceeding under changed authority.
