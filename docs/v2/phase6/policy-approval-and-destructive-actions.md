# Policy Class, Approval Class and Destructive-Action Marking

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Refines `../adrs/ADR-0011-per-node-policy.md` and
> `../adrs/ADR-0012-approval-immutable-plan-digest.md`. See ADR-0025, ADR-0027.

## Policy class

Every runbook declares a `policy_class`, drawn from the V2 policy taxonomy:

| `policy_class` | Meaning |
|---|---|
| `READ_ONLY` | No node may mutate external state |
| `MUTATING_LOW` | Reversible, narrowly scoped mutations (e.g. create a branch) |
| `MUTATING_HIGH` | Mutations with broad or hard-to-reverse effects (e.g. governed merge) |
| `RESTRICTED` | Requires an explicit allow-list of principals in addition to everything else |

**Aggregate rule:** the runbook's effective class is
`max(declared_class, max(node_classes))`. A runbook can only be *stricter* than
its strictest node, never looser. If a declared class is weaker than the
computed aggregate, admission fails with `RB_POLICY_CLASS_TOO_WEAK`. Per-node
policy still evaluates independently at execution; the runbook class does not
replace it and cannot pre-authorize a node.

Absence of a policy entry for any node is a DENY (`RB_POLICY_MISSING`), never a
default-allow.

## Approval class

| `approval_class` | Requirement |
|---|---|
| `NONE` | Permitted only when `policy_class = READ_ONLY` and `destructive_action = false` |
| `SINGLE` | One human approval bound to `plan_digest`, single-use, expiring |
| `DUAL` | Two distinct human approvers; the requester may not be an approver |
| `OWNER_PLUS_SECURITY` | One approval from the runbook `owner` role and one from the security role |

Rules:

- `destructive_action = true` forces `approval_class ≥ DUAL`. A manifest that
  marks a destructive action with `NONE` or `SINGLE` is rejected with
  `RB_APPROVAL_CLASS_TOO_WEAK`.
- `MUTATING_HIGH` forces `approval_class ≥ DUAL`.
- Approvals are bound to the immutable `plan_digest` (see
  `plan-digest-binding.md`), are single-use, expire, carry a nonce and are
  consumed atomically (V2-SEC-005, V2-SEC-011). Approval for one plan can never
  execute another.
- Approval is consumed **once per execution attempt**, before credential
  resolution. A retried execution requires a fresh approval unless the retry is
  an idempotent replay of an already-consumed attempt with an identical digest
  and idempotency key.
- Node-level approvals declared by Phase 3 semantics (e.g. governed merge)
  still apply **inside** the runbook. A runbook-level approval does not satisfy
  a node-level approval requirement; both are required.

## `destructive_action` marker

`destructive_action` is a mandatory boolean on every runbook and on every node
reference in the IR.

A node is destructive if it can, in any reachable path, cause any of:

- deletion or overwrite of external state that the runbook cannot restore from
  data it holds;
- state change to a shared/default resource (default branch, production
  configuration);
- an irreversible provider-side effect (published release, sent message,
  external notification);
- privilege, permission or protection change.

Rules:

1. The marker is **computed** at admission from the node set and compared with
   the declaration. A declared `false` with a computed `true` is rejected with
   `RB_DESTRUCTIVE_UNDERDECLARED`. A declared `true` with a computed `false` is
   accepted (over-declaration is allowed, and is conservative).
2. `destructive_action = true` implies: `approval_class ≥ DUAL`,
   `rollback_support` explicitly declared (including
   `NOT_SUPPORTED`), `retry_class = NO_RETRY` for the destructive node, and
   mandatory write-ahead audit before the node executes.
3. When `rollback_support = NOT_SUPPORTED` and `destructive_action = true`, the
   runbook additionally requires an explicit registry-level
   `accepted_irreversibility` record naming the owner and the accepting
   authority. Without it, admission fails with `RB_IRREVERSIBLE_UNACCEPTED`.
4. Marker changes false→true are MAJOR version bumps
   (`registry-identity-and-versioning.md`).
5. Repository deletion and administrative mutations remain unreachable by
   capability (ADR-0023); the marker governs the destructive operations that
   *are* in scope, and never becomes a way to opt into excluded ones.

## Resource scopes

The runbook declares a resource scope expression. The effective scope is the
intersection of runbook scope, caller scope and policy scope. Out-of-scope
resolution is denied before credential resolution, with zero HTTP requests
(inherits Phase 3 A3-09).

## Denial codes (policy/approval)

`RB_POLICY_MISSING`, `RB_POLICY_DENIED`, `RB_POLICY_CLASS_TOO_WEAK`,
`RB_APPROVAL_REQUIRED`, `RB_APPROVAL_CLASS_TOO_WEAK`, `RB_APPROVAL_EXPIRED`,
`RB_APPROVAL_DIGEST_MISMATCH`, `RB_APPROVAL_ALREADY_CONSUMED`,
`RB_APPROVAL_SELF_APPROVAL`, `RB_DESTRUCTIVE_UNDERDECLARED`,
`RB_IRREVERSIBLE_UNACCEPTED`, `RB_SCOPE_DENIED`.

Reason codes are stable, enumerable and safe to log; they never contain
resource-specific secret material.
