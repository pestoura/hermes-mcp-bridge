# Phase 5 Preparation Lane — DAG Execution Engine

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Gate `BATCH_ACCEPTED` (Phase 4) is **NOT DECLARED**. No Phase 4 lane,
> scheduler, worker pool or aggregation code exists in the repository at the
> time of writing. Nothing in this lane may be implemented, wired into an
> executable path, or treated as acceptance. The operational V1 surface remains
> exactly **27 tools**, bridge `1.0.0`, schema `0.6.1`, contract `1.0.0`.

## Reconciled live state (verification date 2026-08-09)

| Fact | Observed value |
|---|---|
| `main` head | `690f004` — `feat(v2): Phase 3 lane L5 — DIRECT GitHub mutation executor (#89)` |
| Phase 0 | `BASELINE_ACCEPTED` |
| Phase 1 | `REGISTRY_ACCEPTED` |
| Phase 2 | `DIRECT_READ_ACCEPTED` (INNER) + OUTER `ACCEPTED` on `818c56a467ed00b1412a219c78e3c68007848df3` |
| Phase 3 | Lanes C-1, L1–L5 merged (#84–#89). `DIRECT_MUTATION_ACCEPTED` **NOT DECLARED** |
| Phase 4 | **No lane, no docs, no code.** `BATCH_ACCEPTED` NOT DECLARED |
| Phase 5 | This lane only (design). `DAG_ACCEPTED` NOT DECLARED |

Consequence: Phase 5 design is written **in parallel** and is explicitly
downstream of both `DIRECT_MUTATION_ACCEPTED` and `BATCH_ACCEPTED`. Where this
lane needs a Phase 4 primitive that does not yet exist (worker pool, budgets,
backpressure, circuit breakers, partial-success aggregation) it states the
**required interface** as an assumption, marked `ASSUMPTION-P4-nn`, and does not
invent Phase 4 acceptance.

## Hard preconditions (all must hold before any Phase 5 implementation)

1. `DIRECT_MUTATION_ACCEPTED` declared with `failures=[]` (INNER + OUTER).
2. `BATCH_ACCEPTED` declared with `failures=[]`, including bounded worker pool,
   per-provider/credential limits, budgets and partial-success aggregation.
3. A durable store decision for checkpoints/leases/idempotency is made
   (OD-003) and a canonical serialization decision is made (OD-018).
4. The transformation node operation set is fixed (OD-024) with no general code
   execution, no shell surface, no HTTP surface.
5. The Phase 5 test matrix (`test-plan.md`) is implemented and failing-closed
   before any scheduler is wired to a real provider.

## Scope boundary — what Phase 5 is NOT

- **No generic shell surface.** No node type may execute a command line,
  interpret a shell string, or reach `subprocess`.
- **No generic HTTP surface.** Nodes call only registry-declared typed tools
  (ADR-0003, ADR-0004). There is no `http.request` node, no URL argument that
  a caller can point anywhere, no proxying of arbitrary requests.
- **No arbitrary expression evaluation.** Bindings and transforms are a closed,
  typed operation set (ADR-0009, OD-024). No `eval`, no template engine, no
  JSONPath-with-functions, no user-supplied code.
- **No new credential authority.** DAG nodes inherit exactly the Phase 1–3
  capability/credential model; a plan cannot widen scope.
- **No agentic escalation.** That is Phase 8 (`HYBRID_ACCEPTED`).

## Documents in this lane

| Document | Scope |
|---|---|
| `plan-definition.md` | `PlanDefinition`, node/step schema, dependencies, typed bindings, budgets |
| `dag-validation.md` | Static validation, cycle detection, reachability, binding type checking, fail-closed order |
| `scheduling.md` | Bounded parallel topological scheduling, readiness, admission, cancellation, deadlines |
| `checkpoint-and-resume.md` | Persisted checkpoint/resume contract, leases, heartbeats, recovery, dead-letter |
| `per-node-governance.md` | Per-node policy, credential, idempotency and audit inheritance |
| `plan-digest.md` | Deterministic canonical `plan_digest` and approval binding |
| `compensation-and-saga.md` | Compensation/saga semantics for partially executed plans |
| `failure-semantics.md` | Failure propagation, terminal statuses, `INDETERMINATE` behaviour |
| `acceptance-criteria.md` | Fail-closed `DAG_ACCEPTED` criteria (A5-nn) |
| `test-plan.md` | Planned hermetic test matrix and non-runtime fixture layout |
| `dependency-map.md` | Dependency/acceptance map, ADR reconciliation, open decisions |

## Reconciliation with existing V2 documents

This lane does not restate accepted decisions; it refines them for the DAG case.

| Existing document | Relationship |
|---|---|
| `../adrs/ADR-0009-dag-execution-semantics.md` | Governing decision; this lane is the detail behind it |
| `../adrs/ADR-0008-batch-execution-semantics.md` | Phase 4 base; DAG reuses the bounded pool and partial-success model |
| `../adrs/ADR-0011-per-node-policy.md` | Applies unchanged; per-node evaluation is mandatory, not per-plan |
| `../adrs/ADR-0012-approval-immutable-plan-digest.md` | Base decision; specialized here to the DAG `plan_digest` |
| `../adrs/ADR-0013-idempotency-replay-protection.md` | Base decision; extended to node-level keys derived from `plan_digest` |
| `../adrs/ADR-0014-saga-compensation.md` | Base decision; extended to reverse-topological compensation |
| `../adrs/ADR-0003-typed-tools-not-generic-shell.md` | Hard constraint: no shell/HTTP node types |
| `../phase3/approval-and-digest.md` | `operation_digest` for a single mutation; `plan_digest` composes over it |
| `../phase3/rollback-and-compensation.md` | Per-operation compensation table reused as the DAG compensation primitive |
| `../contracts/dag-example.md` | Informal example; `plan-definition.md` supersedes it as the normative schema shape |
| `../requirements/functional.md` | V2-FR-004/005/009/010/011/016/017/019/024 govern this lane |
| `../requirements/security.md` | V2-SEC-003/005/011/012/019 govern this lane |
| `../open-decisions.md` | OD-003, OD-018, OD-021, OD-024 remain open and are explicitly not closed here |
