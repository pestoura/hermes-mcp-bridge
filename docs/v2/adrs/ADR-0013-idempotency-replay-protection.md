# ADR-0013 — Idempotency and Replay Protection

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Network retries or resumed workflows can duplicate mutations such as merges, issues or tasks.

## Decision
Mutating nodes carry stable idempotency keys where feasible; persist key -> execution/result association and classify retries as `RETRY_SAFE`, `RETRY_CONDITIONAL` or `NO_RETRY`.

## Consequences
Requires durable idempotency state and backend-specific semantics.

## Alternatives
Blind retry; never retry any mutation.

## Security implications
Reduces replay/duplicate mutation risk but stored keys must be scope-bound and protected from collision/abuse.

## Operational implications
Retry logic honors provider rate limits, `Retry-After`, bounded backoff/jitter and global deadline.

## Open questions
Retention window and idempotency key namespace rules.
