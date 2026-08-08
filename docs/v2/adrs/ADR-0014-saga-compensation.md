# ADR-0014 — Saga and Compensation

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Cross-system workflows cannot rely on ACID transactions.

## Decision
Reuse/extend saga semantics: mutating nodes explicitly declare compensatability, compensation operation/safety/evidence; failure may choose roll-back, roll-forward, partial-success or manual intervention.

## Consequences
Recovery semantics become explicit but backend-specific and sometimes manual.

## Alternatives
Pretend cross-system transactions are atomic; always rollback automatically.

## Security implications
Compensation is itself a privileged mutation and must be policy/approval/idempotency governed.

## Operational implications
Evidence must show committed and compensated states; unsafe compensation dead-letters to manual intervention.

## Open questions
Default failure strategy by tool/security tier.
