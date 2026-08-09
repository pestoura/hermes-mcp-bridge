# Deterministic `plan_digest` (Design)

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Canonical serialization format is **OD-018 (open)**. This document fixes the
> semantics; the encoding choice must be made before implementation.

## Purpose

`plan_digest` is the immutable identity of a plan. It binds approvals
(V2-SEC-005), scopes idempotency keys, keys checkpoints, and makes review
diffs meaningful. Two plans with identical execution semantics must produce the
same digest; any semantic difference must produce a different digest.

## Canonical form

```text
plan_digest = SHA-256( canonical_bytes( CanonicalPlan ) )
```

`CanonicalPlan` is derived from `PlanDefinition` by:

1. **Field allow-list.** Only semantically significant fields are included.
   Unknown fields are rejected at validation, so they can never reach here.
2. **Exclusion of editorial metadata.** Descriptions, labels, comments and any
   human-readable annotation are excluded (Phase 1 precedent). They may be
   carried in the request but must not affect the digest — otherwise a comment
   edit invalidates an approval.
3. **Exclusion of volatile fields.** `plan_id` supplied by the caller,
   timestamps, `execution_id`, nonces, client hints and transport metadata are
   excluded.
4. **Inclusion of the resolved binding graph.** Node ids, `depends_on` (sorted,
   deduplicated), binding source paths and declared types are included.
5. **Inclusion of governance-relevant fields.** `tool`/`op`, literal `args`,
   `policy_ref`, `idempotency` configuration, `on_failure`, `retry_ref`,
   `compensation`, `budget`, `failure_policy`, `rollback_policy`,
   `deadline_ms`, `schema_version`.
6. **Inclusion of the engine contract version.** A digest algorithm or
   canonicalization change bumps `digest_version`, which is prefixed into the
   hashed bytes. Digests from different `digest_version` values never compare
   equal.

## Determinism rules

| Rule | Reason |
|---|---|
| Object keys sorted lexicographically by code point | encoding-independent order |
| Arrays that are semantically sets (`depends_on`) sorted and deduplicated | ordering is not semantics |
| Arrays that are semantically sequences (`nodes`) sorted by `node_id` | node order in the request is not semantics; the graph is |
| No floating point; integers only, bounded range | no representation ambiguity |
| Strings NFC-normalized, UTF-8, rejected if they contain control characters | homoglyph/encoding ambiguity |
| Explicit `null` and absent field are the same and are omitted | one representation per meaning |
| Booleans and enums encoded as their canonical token, never as 0/1 | no coercion ambiguity |
| Nested depth and total canonical byte size bounded | hashing DoS control |

Consequence to test explicitly: reordering `nodes` in the request, reordering
`depends_on`, adding a description, or reformatting whitespace must **not**
change the digest. Changing a tool, an argument value, a dependency edge, a
budget ceiling, a failure policy or a `digest_version` **must** change it.

## Approval binding

```text
approval.digest == computed plan_digest   (exact)
approval.nonce  unused, atomically consumed (compare-and-set)
approval.expires_at > now
approval.scope  ⊇ union of every node's resolved resource scope
```

Failure of any check is DENY with a stable reason code. Consumption is atomic
and single-use (V2-SEC-011); concurrent consumption attempts must produce
exactly one success. A resumed execution does not re-consume its approval; the
consumed `approval_ref` is stored in the checkpoint.

Approval covers the plan as validated. If any node's resolved argument set
changes because an upstream result differs on a re-run, the digest is unchanged
(digest covers literals and the binding graph, not runtime values) — therefore
**runtime-value-sensitive mutations must additionally bind the Phase 3
`operation_digest`** at the node, computed over resolved arguments, and a node
whose `operation_digest` was not covered by the approval is DENIED. This is the
explicit composition rule between Phase 3 and Phase 5.

## Composition with `operation_digest`

```text
plan_digest       : static plan identity, computed pre-execution
operation_digest  : per-mutation identity over resolved args, computed at dispatch
node key          : H(plan_digest || node_id || attempt_epoch || canonical(resolved_args))
```

For approval-gated mutating nodes, the approval must enumerate either the exact
expected `operation_digest` values (fully-literal plans) or an explicit
`runtime_bound: true` acknowledgement plus a resource-scope constraint that the
resolved arguments must satisfy. Without one of the two, the node is DENIED.

## Requirements traced

V2-SEC-005, V2-SEC-011, V2-SEC-019, V2-FR-018, ADR-0012, ADR-0013, ADR-0021.
Open: OD-018 (canonical serialization), OD-008 (approval UX).
