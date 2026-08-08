# ADR-0012 — Approval Bound to Immutable Plan Digest

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Approvals can be replayed or become unsafe if an authorized plan changes between review and execution.

## Decision
Canonicalize the operation/DAG/runbook plan, compute `plan_digest`, bind approval to principal/scope/operation/arguments/digest/expiry/nonce/trust context and atomically consume authorization during execution.

## Consequences
Any semantic plan change invalidates approval; canonical serialization is critical.

## Alternatives
Approval by plan ID only; mutable approval records; broad session approvals.

## Security implications
Mitigates replay, TOCTOU and approval-of-A-used-for-B.

## Operational implications
Need digest migration/versioning and clear UX when plans change.

## Open questions
Canonical serialization format and approval UX.
