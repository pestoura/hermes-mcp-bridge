# Phase 4 Dependency Map

> **V2 · PHASE 4 · DESIGN · unblocked by `DIRECT_MUTATION_ACCEPTED` (a86b26d) · runtime gated behind `BATCH_FEATURE_ENABLED` until `BATCH_ACCEPTED`**

## Blocking dependency

Phase 4 implementation is blocked on the Phase 3 gate
`DIRECT_MUTATION_ACCEPTED` (`../phase3/acceptance-criteria.md`). No BATCH
runtime code, no `mode="BATCH"` dispatch, no scheduler module may be merged
before that gate is declared with `failures=[]`.

This lane is design/test-plan/docs only and is therefore safe to review in
parallel, but the PR carrying it is marked `DO_NOT_MERGE UNTIL
DIRECT_MUTATION_ACCEPTED`.

## Consumed from Phase 3 (read-only, unchanged)

| Phase 3 asset | Used by Phase 4 for |
|---|---|
| DIRECT execution path (L5 executor) | Executes each step; batch adds no provider logic |
| Typed mutation registry (L2) | Step `tool` resolution and arg validation |
| Write-credential split (L1) | Per-step credential resolution |
| `mutation_digest` / approval binding | Per-step digest and approval validation |
| `mutation_idempotency` | Per-step idempotency, replay, conflict |
| `mutation_audit` | Per-step audit record + batch envelope record |
| Redaction / evidence path | Step errors and batch evidence |
| Observability surface | Batch counters and histograms |

## Explicitly NOT touched by this lane

- Any Phase 3 lane file under `docs/v2/phase3/`.
- Controller-owned `docs/v2/roadmap.md`.
- Controller-owned `docs/v2/requirements/traceability-matrix.md`.
- `docs/v2/adrs/ADR-0008-batch-execution-semantics.md` and
  `docs/v2/contracts/batch-example.md` — refined here, left unmodified there.
- Any file under `src/` and any existing test module.

Promotion of Phase 4 into roadmap/traceability is a separate Controller-approved
change once the design is accepted.

## Implementation waves (post-gate, for reference only)

| Wave | Content | Depends on |
|---|---|---|
| P4-W1 | Typed models + validation + `DENIED` paths, no execution | Gate |
| P4-W2 | Scheduler with bounded concurrency + injected executor/clock (S-01..S-08) | P4-W1 |
| P4-W3 | Failure policies, timeouts, fail-closed cancellation (S-09..S-15) | P4-W2 |
| P4-W4 | Per-step governance wiring: policy, approval, idempotency, audit (S-16..S-21) | P4-W3, Phase 3 components |
| P4-W5 | Aggregation, evidence, metrics, redaction (S-22..S-23) | P4-W4 |
| P4-W6 | Preflight prohibited-surface scan, V1 invariance, gate declaration (S-24..S-27) | P4-W5 |

Feature flag defaults to off through P4-W6.
