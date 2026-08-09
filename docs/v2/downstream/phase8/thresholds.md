# Thresholds and Budgets

>
> **V2 · PHASE 8 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires Phases 3–6 accepted and at least two accepted Phase 7 integrations.
> No resolver code, flag or gate change exists.

Values are **placeholders to be calibrated from evidence** (Phase 0 baseline plus
Phase 2/4/5/7 measurements) before `HYBRID_ACCEPTED`. Each must be explicit,
versioned in configuration, recorded in the decision record, and changeable only
through a governed change with a new capability/plan digest.

| Threshold | Symbol | Placeholder | Source of truth |
|---|---|---|---|
| Max batch nodes | `BATCH_MAX_NODES` | 50 | Phase 4 acceptance |
| Max DAG nodes / depth | `DAG_MAX_NODES` / `DAG_MAX_DEPTH` | 200 / 12 | Phase 5 acceptance |
| Max escalations per request | `MAX_ESCALATIONS_PER_REQUEST` | 1 | Phase 8 measurement |
| Agentic token budget | `AGENTIC_TOKEN_BUDGET` | per-request, default 0 (opt-in) | Phase 0 baseline (519,048 tokens across 9 samples) |
| Agentic wall-clock budget | `AGENTIC_DEADLINE_S` | 120 | Phase 8 measurement |
| Deterministic step deadline | `DIRECT_DEADLINE_S` | 10 | Phase 2/3 evidence |
| Result byte budget | `RESULT_MAX_BYTES` | inherited per capability | Phase 2 accepted |
| Minimum deterministic coverage to claim HYBRID benefit | `MIN_DET_COVERAGE` | 0.5 | Phase 8 measurement |

## Rules

1. Zero default agentic budget: absence of an explicit allowance is a refusal
   (`E-AGENTIC-NOT-ALLOWED`), never an implicit escalation.
2. Budget exhaustion is a refusal with partial results returned explicitly
   marked, never a silent truncation.
3. Thresholds never relax safety controls; they only bound cost and size.
4. A threshold change is recorded and invalidates prior economics comparisons,
   which must be re-measured.
