# Phase 8 — HYBRID Execution (design lane)

>
> **V2 · PHASE 8 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires Phases 3–6 accepted and at least two accepted Phase 7 integrations.
> No resolver code, flag or gate change exists.

HYBRID is not "let the model decide when it wants to". It is a **deterministic
resolver** that selects the cheapest sufficient execution mode and escalates to
reasoning only through an explicit, recorded branch.

| Document | Scope |
|---|---|
| `resolver-decision-tree.md` | The normative decision tree DIRECT → BATCH/DAG/RUNBOOK → AGENTIC |
| `thresholds.md` | Numeric thresholds, budgets and where their values come from |
| `reason-codes.md` | Complete enumerated escalation/refusal reason codes |
| `evidence-and-economics.md` | Token/cost/latency measurement contract |
| `safety-invariants.md` | Non-downgrade rules and refusal semantics |
| `test-matrix.md` | Required resolver tests |
| `acceptance-criteria.md` | Fail-closed `HYBRID_ACCEPTED` criteria |
