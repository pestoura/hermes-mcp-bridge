# ADR-0027 — Computed `destructive_action` Marker and Declared Rollback Support

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

**Status:** Proposed

## Context
ADR-0014 establishes saga/compensation semantics and V2-SEC-021 requires that
compensations be independently governed and never assumed safe. In a multi-node
runbook the dangerous property is emergent: individually reversible nodes can
compose into a workflow whose partial failure leaves state no single
compensation can restore. An author-declared "this is safe" is not evidence.

## Decision
`destructive_action` is mandatory on the runbook and on every node, and is
**computed** at admission from the node set, then compared with the declaration:
under-declaration is rejected, over-declaration is accepted. A node is
destructive if any reachable path can delete or overwrite unrestorable external
state, change a shared/default resource, cause an irreversible provider-side
effect, or change privileges/protection. `destructive_action = true` forces
`approval_class ≥ DUAL`, `NO_RETRY` on the destructive node, mandatory
write-ahead audit, and an explicit `rollback_support` value.

`rollback_support` is a declaration, never an inference:
`NOT_APPLICABLE | AUTOMATIC | MANUAL | NOT_SUPPORTED`. `AUTOMATIC` requires a
registered compensation that is itself a governed mutation with its own policy,
audit and idempotency, proven by test. The runbook's aggregate value is the
**weakest** across its mutating nodes. `NOT_SUPPORTED` combined with
`destructive_action = true` requires a recorded `accepted_irreversibility` naming
the owner and the accepting authority. Compensation runs in reverse order over
nodes proven committed by write-ahead audit records; when safety cannot be
proven the execution dead-letters to `MANUAL_INTERVENTION_REQUIRED` without
attempting a write, and partial compensation reports `COMPENSATED_PARTIAL` with
the exact residual list.

## Consequences
Some workflows will be admissible only with dual approval and no retry, which is
slower. Some will be inadmissible until an irreversibility acceptance exists.
Authors cannot make a workflow cheaper by describing it optimistically.

## Alternatives
Trust the declared marker; infer destructiveness at runtime; attempt best-effort
rollback whenever anything fails; treat compensation as an internal action
exempt from policy.

## Security implications
Addresses V2-SEC-021 and inherits ADR-0023's exclusion of administrative and
deletion operations by capability. Refusing to write when compensation safety is
unproven prevents a failed rollback from becoming a second incident.

## Operational implications
Operators need a clear dead-letter queue, an accurate residual object report and
a documented manual procedure for every `MANUAL` declaration. Reverse-order
compensation depends on the write-ahead audit store being append-only and
complete.

## Open questions
Whether forward-fix operations (for example an explicit revert) should be
offered as an alternative to compensation for some node classes, and the
retention of dead-lettered executions alongside OD-010.
