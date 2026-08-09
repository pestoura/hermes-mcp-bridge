# Phase 4 Preparation Lane — BATCH Execution

> **V2 · PHASE 4 · DESIGN ONLY · NOT_IMPLEMENTED · DO_NOT_MERGE UNTIL DIRECT_MUTATION_ACCEPTED**
>
> Nothing in this lane may be implemented, wired into an executable path,
> exported from `hermes_mcp_bridge`, or treated as acceptance. The operational
> V1 surface remains exactly **27 tools**, bridge `1.0.0`, schema `0.6.1`,
> contract `1.0.0`. No runtime BATCH code exists and none may be added while
> the Phase 3 gate `DIRECT_MUTATION_ACCEPTED` is not declared with
> `failures=[]`.

## Purpose

Hold the complete BATCH design — typed contract, budgets, concurrency,
failure/cancellation semantics, audit reuse and acceptance scenarios — so that
Phase 4 implementation can start immediately after `DIRECT_MUTATION_ACCEPTED`
without inventing execution or security semantics under delivery pressure.

This lane refines, and does not replace, `../adrs/ADR-0008-batch-execution-semantics.md`
and `../contracts/batch-example.md`, which remain the higher-level statements
of intent. Where this lane is more specific, it is normative for Phase 4.

## Hard preconditions (all must hold before any Phase 4 implementation)

1. `DIRECT_MUTATION_ACCEPTED` declared with `failures=[]` from the Phase 3
   acceptance gate (`../phase3/acceptance-criteria.md`).
2. Phase 3 DIRECT single-operation execution is the *only* execution path in
   the tree; BATCH is strictly a scheduler above unchanged DIRECT steps.
3. Per-step policy, idempotency, digest/approval and audit components are
   reused unmodified (`step-governance.md`); no BATCH-specific bypass exists.
4. The acceptance scenarios in `acceptance-scenarios.md` are implemented and
   failing-closed before any concurrency is enabled.

## Documents in this lane

| Document | Scope |
|---|---|
| `contract.md` | Typed `BatchRequest` / `BatchStep` / `BatchResult` / `BatchStepResult` |
| `limits-and-budgets.md` | `max_items`, `max_parallelism`, timeout hierarchy, backpressure |
| `concurrency-and-scheduling.md` | Bounded concurrency, ordering, non-serial independent steps |
| `failure-and-cancellation.md` | Partial failure, fail-closed cancellation, no compensation |
| `step-governance.md` | Per-step policy, idempotency, approval and audit reuse |
| `aggregation-and-evidence.md` | Aggregate status algebra, evidence record, redaction |
| `non-goals.md` | Explicit exclusions: generic shell, generic HTTP, DAG, retries |
| `acceptance-scenarios.md` | Fail-closed `BATCH_ACCEPTED` criteria and scenarios |
| `dependency-map.md` | What Phase 4 consumes from Phase 3 and what it must not touch |

## Ownership boundary

This lane is self-contained under `docs/v2/phase4/`. It does not edit
Phase 3 lane files, Controller-owned `../roadmap.md`, or
`../requirements/traceability-matrix.md`. Promotion of Phase 4 into those
Controller-owned documents is a separate, Controller-approved change.
