# ADR-0022 — GitHub Mutation Idempotency and Optimistic Concurrency

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

**Status:** Proposed

## Context
ADR-0013 sets the general idempotency model. GitHub's REST write endpoints have
no generic idempotency-key mechanism, so duplicate-write protection must be
constructed from server-side derived keys, write-ahead records and
provider-specific preconditions.

## Decision
Derive the idempotency key server-side from principal, capability, repository,
operation and `operation_digest`; write the intent record **before** the
provider call; classify records as `IN_PROGRESS`, `COMMITTED`, `FAILED_CLEAN` or
`AMBIGUOUS`. Pair every mutation with an expected-state precondition and push
the check to the provider where possible (`sha` on merge, natural `422` on ref
creation). Retry classes: `RETRY_CONDITIONAL` for create operations behind a
safe existence probe, `NO_RETRY` for merge.

## Consequences
Durable idempotency and lease state is required before any mutation ships.
Ambiguous outcomes are resolved by reading provider state, never by retrying.

## Alternatives
Caller-supplied idempotency keys as the primary scope (rejected: caller could
widen or collide); blind retry with backoff (rejected: duplicates merges).

## Security implications
Addresses T3-01, T3-03, T3-13 and T3-15. Server-side key scoping prevents
cross-principal record reuse.

## Operational implications
Retention windows, lease TTLs and reconciliation tooling become operational
concerns; OD-011 (retry defaults) is closed by this ADR only for GitHub Phase 3.

## Open questions
Retention window values and the storage backend for idempotency/lease state
(interacts with OD-003).
