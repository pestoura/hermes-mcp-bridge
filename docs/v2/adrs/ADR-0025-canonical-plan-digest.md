# ADR-0025 — Canonical Plan Digest (closes OD-018 for plans)

> **V2 · PHASE 5 · IMPLEMENTED BEHIND `DAG_FEATURE_ENABLED` · NO V1 IMPACT**

**Status:** Accepted (Phase 5)

## Context
OD-018 left the canonical serialization format open. ADR-0012 binds approvals to
an immutable plan digest and ADR-0021 defines `operation_digest` as its
single-node specialization, both of which are meaningless without one fixed,
shared canonicalization. Two encodings would mean two digests for one plan.

## Decision
Canonical form is **JSON, UTF-8, sorted keys, fixed separators, integers only,
no floats**, reusing `v2/canonical.py` — the primitive already proven by the
accepted Phase 1 snapshot gate. The hashed byte stream is prefixed with
`digest_version = "dagdigest/1"`, so digests produced under different
canonicalization rules can never compare equal. `plan_digest` covers exactly the
semantically significant fields: schema version, mode, failure and rollback
policy, deadline, dry-run, the full budget, and every node's id, kind, tool/op,
literal args, bindings (target, source, type, required, max_bytes), sorted
`depends_on`, `on_failure`, timeout, policy/retry refs, idempotency and declared
compensation. It deliberately excludes `plan_id`, descriptions and any editorial
metadata, and excludes `approval` (an approval cannot bind to itself).
`operation_digest(node_id, resolved_args)` composes with it for per-node
mutation approval, and `node_idempotency_key` is
`H(plan_digest ‖ node_id ‖ attempt_epoch ‖ canonical(resolved_args))`.

## Consequences
Reordering nodes or renaming a plan does not change the digest (A5-07); changing
a repository, an edge, a budget or the failure policy does. One canonicalizer
serves Phase 1, 3, 4 and 5, so a change to it is a single reviewable, versioned
event rather than a silent divergence.

## Alternatives
* **CBOR / deterministic binary encoding** — rejected: a second encoding to keep
  in sync, float and tag ambiguity, and not diffable in review.
* **Hash of the raw request body** — rejected: whitespace and key order would
  change the digest, making approvals fragile and meaningless.
* **Digest including `plan_id`** — rejected: a volatile caller-supplied label
  would invalidate approvals for an identical plan.

## Security implications
Addresses T3-02/T3-03 at plan scope: any semantic edit invalidates an
outstanding approval. Excluding editorial fields prevents an attacker from
changing behaviour while keeping the digest — because those fields have no
behaviour — and including bindings prevents rewiring data flow under a stable
digest.

## Operational implications
`digest_version` must be bumped whenever canonicalization semantics change; all
outstanding approvals are then invalid by construction, which is the intended
fail-closed behaviour.

## Open questions
None for plans. Runbook digest binding is Phase 6 (ADR-0028).
