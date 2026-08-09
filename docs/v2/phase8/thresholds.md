# Thresholds and Budgets

>
> **V2 · PHASE 8 · implemented, disabled by default behind `HYBRID_FEATURE_ENABLED`**
>
> Phases 3-6 are accepted and Phase 7 accepted two integrations (`github`,
> `jira`), so the prerequisite holds. The resolver ships with a zero agentic
> token budget: absence of an explicit allowance is a refusal.

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
| Minimum deterministic coverage to claim HYBRID benefit | `min_deterministic_coverage_permille` | 500 (‰) | Phase 8 measurement |

## Encoding note

Coverage and thresholds are carried as **integers in permille**, never floats.
The canonical encoder rejects floats by design (they have no stable
cross-platform encoding), and a decision digest that depended on float
repr would not be reproducible. `deterministic_coverage_permille` is therefore
the recorded field; the float ratio exists only as a convenience view that never
enters a canonical form or a digest.

## Rules

1. Zero default agentic budget: absence of an explicit allowance is a refusal
   (`E-AGENTIC-NOT-ALLOWED`), never an implicit escalation.
2. Budget exhaustion is a refusal with partial results returned explicitly
   marked, never a silent truncation.
3. Thresholds never relax safety controls; they only bound cost and size.
4. A threshold change is recorded and invalidates prior economics comparisons,
   which must be re-measured.
