# ADR-0037 - Escalation may relax determinism, never safety

- Status: Accepted (Phase 8)
- Supersedes: the `ADR-0028` proposal in the downstream design lane.
- Related: ADR-0033 (credential domains), Phase 3 approval digest binding.

## Context

The tempting failure mode of a hybrid system is to "try harder" when a
deterministic path is unavailable — retrying a policy-denied intent in another
mode, or treating a DEGRADED write as usable under time pressure.

## Decision

Invariants I1-I10 hold in **every** mode including AGENTIC. Concretely, in the
implementation: a policy `DENY` returns before any branch is considered; an
escalated plan is a *new intent* with a new digest, so an approval bound to the
old digest is void; the escalation counter is checked before and after the
agentic gate; a write capability that is not `READY` can never be selected for
DIRECT and never becomes eligible by escalating.

The agentic gate is conjunctive — allowance, budget, approval, policy and
context shaping must *all* hold. Any missing condition is a refusal with its own
code, never a downgrade.

## Consequences

- A request that could only succeed by weakening a control fails with
  `E-SAFETY-DOWNGRADE-REFUSED` or the specific blocking code.
- Budget exhaustion returns partial results **explicitly marked** partial; silent
  truncation is not representable in `HybridOutcome`.
