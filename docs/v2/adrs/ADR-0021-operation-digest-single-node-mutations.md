# ADR-0021 — Operation Digest for Single-Node Mutations

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

**Status:** Proposed

## Context
ADR-0012 binds approvals to an immutable plan digest for DAG/runbook execution.
Phase 3 executes a single DIRECT mutation and therefore has no plan, but needs
the identical anti-replay and anti-TOCTOU guarantees.

## Decision
Define `operation_digest` as the single-node specialization of the plan digest.
It covers the canonical tool id, capability, repository, fully-resolved
arguments, expected-state preconditions (`base_sha` / `expected_head_sha`),
`policy_version` and `registry_snapshot_hash`. Approvals bind to this digest and
are single-use, scoped, expiring and atomically consumed.

## Consequences
Any semantic change — including an edited PR body, a moved head SHA, a policy
change or a registry change — invalidates outstanding approvals. Canonical
serialization must be shared with the accepted Phase 1 snapshot canonicalization.

## Alternatives
Approve by operation name and repository only; session-scoped approvals; digest
over arguments alone without preconditions or versions.

## Security implications
Addresses T3-02 and T3-03. Including the expected SHAs makes the approval bind
observed *state*, not just intent.

## Operational implications
Approvers may see approvals invalidated by upstream activity; the UX must show
why (OD-008 remains open).

## Open questions
Exact TTLs per operation; canonical serialization format is OD-018 and must be
reused rather than re-decided here.
