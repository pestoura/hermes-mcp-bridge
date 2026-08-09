# `plan_digest` Binding

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Specializes `../adrs/ADR-0012-approval-immutable-plan-digest.md` and the
> Phase 3 `operation_digest` (ADR-0021) to runbook executions. See ADR-0025.

## Two digests, one chain

| Digest | Covers | Stable across |
|---|---|---|
| `runbook_digest` | The runbook definition IR (identity, graph, schemas, classes, pins) | All executions of that version |
| `plan_digest` | A specific *execution plan*: this runbook version, these arguments, this scope, this control-plane state | One approved execution |

`plan_digest` binds `runbook_digest`; a runbook change necessarily changes every
derived plan digest.

## `plan_digest` inputs

```
plan_digest = SHA-256(canonical_bytes({
  "digest_schema_version":     <int>,
  "runbook_id":                <string>,
  "runbook_version":           <semver>,
  "runbook_digest":            <sha256>,
  "resolved_arguments":        <canonical, defaults materialized, sensitive
                                values represented by a salted commitment,
                                never plaintext>,
  "resolved_resource_scope":   <canonical, fully expanded — no wildcards>,
  "effective_capabilities":    <sorted capability IDs actually to be used>,
  "capability_snapshot_hash":  <sha256>,
  "runbook_snapshot_hash":     <sha256>,
  "policy_class":              <enum>,
  "approval_class":            <enum>,
  "destructive_action":        <bool>,
  "resolved_node_plan":        <ordered node list with tool (name, version),
                                per-node policy decision inputs, timeouts,
                                retry class, idempotency key derivation>,
  "budgets":                   <effective, after caller tightening>,
  "principal_ref":             <caller identity reference>,
  "expected_preconditions":    <e.g. expected head SHA, expected branch state>
}))
```

Excluded from `plan_digest`: wall-clock time, request IDs, trace IDs, retry
counters, and anything else that would make an unchanged plan hash differently.

`resolved_arguments` uses fully materialized values: two requests that differ
only because one relied on a default must produce the **same** digest. Sensitive
parameters contribute a commitment (salted hash), never plaintext, so the digest
is stable without leaking values into approval records or evidence.

## Binding chain

```
runbook manifest
  └─ admission ─> canonical IR ─> runbook_digest
                                    │
        caller arguments + scope ───┤
        control-plane snapshots ────┼─> plan_digest
        principal + preconditions ──┘
                                    │
                              approval(s) bound to plan_digest
                                    │  (single-use, expiring, nonce)
                                    ▼
                          atomic consumption at execution
                                    │
                                    ▼
                     execution recorded with both digests
```

## Enforcement rules

1. **Digest required.** The invocation must carry the expected `runbook_digest`
   and, when approval is required, the `plan_digest` the approval was issued
   for. Missing → `RB_DIGEST_REQUIRED`.
2. **Recompute, never trust.** The engine recomputes `plan_digest` from the
   actual resolved plan at execution time and compares. Mismatch →
   `RB_APPROVAL_DIGEST_MISMATCH`, deny, zero credential resolution.
3. **Any semantic change invalidates.** Different argument, different scope,
   different tool version, changed capability snapshot, changed policy class,
   changed runbook version — all produce a different digest and therefore an
   invalid approval.
4. **Single use and atomic.** Consumption is atomic; concurrent consumption has
   exactly one winner, the loser gets `RB_APPROVAL_ALREADY_CONSUMED`
   (V2-SEC-011).
5. **Expiry and nonce.** Expired → `RB_APPROVAL_EXPIRED`. A replayed nonce is
   rejected.
6. **Scope-for-A cannot execute B.** Approval carries the full plan digest, so
   an approval for repository A cannot authorize repository B even with the
   same runbook and arguments shape.
7. **Precondition drift.** `expected_preconditions` (e.g. `expected_head_sha`)
   are pushed to the provider where the provider supports it, and otherwise
   re-verified immediately before the mutating node. Drift → deny or provider
   `409`, never a silent write (inherits A3-08).
8. **Node-level digests still apply.** Phase 3 `operation_digest` binding
   remains in force per mutating node inside the runbook; the runbook digest
   does not replace it.

## Versioning of the digest itself

`digest_schema_version` is explicit. Digests are only comparable within the same
schema version; a schema-version change invalidates outstanding approvals by
construction. Canonical serialization is OD-018 and must be shared with the
DAG plan digest so that a runbook plan and an equivalent DAG plan hash
identically — this is what makes the migration path in
`migration-dag-to-runbook.md` verifiable.
