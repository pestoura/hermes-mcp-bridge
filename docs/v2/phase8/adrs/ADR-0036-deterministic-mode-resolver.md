# ADR-0036 - Execution mode is chosen by a pure deterministic resolver

- Status: Accepted (Phase 8)
- Supersedes: the `ADR-0027` proposal in the downstream design lane, whose number
  was taken by an accepted Phase 5 decision.
- Related: ADR-0034 (demote-only discovery), ADR-0025 (canonical plan digest).

## Context

If an LLM chooses the execution mode, the safety posture of a request becomes a
sampling outcome. Determinism is not a performance preference here; it is what
makes the decision auditable and replayable.

## Decision

Mode selection is a **pure total function** of the typed request, the capability
snapshot (plus its digest), the policy result and the declared budgets. The
resolver imports no clock, no randomness, no environment and no I/O module; the
gate enforces this with an AST purity scan over `v2/resolver.py`.

The walk order is the permanent preference DIRECT > BATCH > DAG/RUNBOOK >
AGENTIC. Every evaluation emits one decision record with exactly one primary
reason code and the ordered list of rejected branches, and the record is
canonically serializable so a replay is byte-comparable.

Determinism is proven, not asserted: the gate performs 100 real replays per
scenario class and fails on any mismatch (`E-RESOLVER-NONDETERMINISM`).

## Consequences

- Coverage and thresholds are integers in permille, because the canonical
  encoder rejects floats and a digest must be reproducible.
- Adding a mode means adding a branch, a reason code and a scenario class; the
  gate's preference walk fails if the new branch is reachable before DIRECT.
