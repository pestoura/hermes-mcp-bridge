# ADR-0029 — Runbook Digest, Plan Digest and Approval Binding

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

**Status:** Proposed

## Context
ADR-0012 binds approval to an immutable canonical plan digest, and ADR-0021
specializes it to single-node mutations as `operation_digest`. A runbook adds a
second stable object — the definition itself — which is reused across many
executions with different arguments. Binding approval to the definition would
authorize every future argument set; binding only to arguments would ignore a
changed definition.

## Decision
Maintain two digests in a chain. `runbook_digest` covers the definition IR and
is stable across executions of a version. `plan_digest` covers a specific
execution: `runbook_digest`, fully resolved arguments (defaults materialized,
sensitive values as salted commitments), fully expanded resource scope,
effective capabilities, `capability_snapshot_hash`, `runbook_snapshot_hash`,
policy/approval class, destructive marker, resolved node plan, effective budgets,
principal reference and expected preconditions. Approvals bind to `plan_digest`,
are single-use, expiring, nonce-protected and atomically consumed. The engine
recomputes `plan_digest` at execution and denies on mismatch. Node-level
`operation_digest` binding remains in force inside the runbook.

## Consequences
Any semantic change — argument, scope, tool version, capability snapshot, policy
class, runbook version — invalidates an outstanding approval. Approval UX must
show a plan diff. Canonical serialization becomes critical infrastructure.

## Alternatives
Approve the runbook version once and allow any arguments; approve by execution
ID; approve per session; rely on node-level digests alone.

## Security implications
Mitigates replay, TOCTOU and approval-of-A-used-for-B across the whole workflow
rather than per node (V2-SEC-005, V2-SEC-011). Salted commitments keep sensitive
arguments out of approval records and evidence while keeping the digest stable.

## Operational implications
Requires `digest_schema_version`, atomic single-winner consumption under
concurrency, and clear operator messaging when a plan changes.

## Open questions
Canonical serialization (OD-018) and approval UX (OD-008) remain open; the
commitment salt lifecycle needs a decision alongside OD-005.
