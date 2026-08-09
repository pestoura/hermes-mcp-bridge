# Phase 5 Dependency and Acceptance Map

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

## Gate dependency chain (observed state, 2026-08-09)

```text
BASELINE_ACCEPTED  (Phase 0) ── ACCEPTED
        │
REGISTRY_ACCEPTED  (Phase 1) ── ACCEPTED
        │
DIRECT_READ_ACCEPTED (Phase 2) ── ACCEPTED (inner + OUTER, 818c56a4)
        │
DIRECT_MUTATION_ACCEPTED (Phase 3) ── NOT DECLARED
        │        lanes C-1, L1..L5 merged (#84..#89); gate evidence pending
        │
BATCH_ACCEPTED (Phase 4) ── NOT DECLARED, NO LANE, NO CODE
        │
DAG_ACCEPTED (Phase 5) ── NOT DECLARED; this lane is design only
```

Phase 5 implementation is blocked on **both** upstream gates. This lane is
parallel design work and creates no executable path.

## Inherited primitives

| From | Primitive | Phase 5 use |
|---|---|---|
| Phase 1 | Canonical registry, capability projection, fail-closed policy engine, capability snapshot hash | Node resolution, per-node policy, projection digest |
| Phase 1 | Credential broker contract (status-only) | Per-node readiness without material exposure |
| Phase 2 | Scope→policy→readiness→authorization→provider ordering; zero-side-effect denial; shaped result allow-lists | Validation ordering, binding sources, denial properties |
| Phase 3 | `operation_digest`, approvals, idempotency, optimistic concurrency, write-ahead audit, compensation table, governed merge, destructive exclusion | Node-level mutation governance, compensation primitives |
| Phase 4 (assumed) | Bounded worker pool, per-provider/credential limits, budgets, backpressure, circuit breakers, partial-success aggregation | Scheduler substrate (ASSUMPTION-P4-01..06) |

## Phase 4 assumptions that must be satisfied by `BATCH_ACCEPTED`

| ID | Requirement on Phase 4 | Blocks |
|---|---|---|
| ASSUMPTION-P4-01 | Hard global concurrency ceiling in a bounded pool | A5-10 |
| ASSUMPTION-P4-02 | Per-provider and per-credential concurrency limits | A5-10 |
| ASSUMPTION-P4-03 | Budget accounting counters | A5-18 |
| ASSUMPTION-P4-04 | Admission rejection instead of unbounded queueing | A5-18 |
| ASSUMPTION-P4-05 | Per-provider circuit breaker with explicit states | A5-19 |
| ASSUMPTION-P4-06 | Deterministic partial-success aggregation | A5-19 |

If Phase 4 lands a different shape, this lane is revised before implementation;
Phase 5 does not re-implement Phase 4 locally.

## Requirement → design → criterion map

| Requirement | Design document | Criterion |
|---|---|---|
| V2-FR-004 explicit dependencies | `plan-definition.md`, `dag-validation.md` | A5-05 |
| V2-FR-005 typed schema-validated bindings | `dag-validation.md` | A5-06 |
| V2-FR-009 checkpoint/resume/recovery | `checkpoint-and-resume.md` | A5-15, A5-16, A5-17 |
| V2-FR-010/011 partial success, aggregation | `failure-semantics.md` | A5-19, A5-12 |
| V2-FR-016 bounded deterministic transforms | `plan-definition.md` | A5-04 |
| V2-FR-017 `dry_run` policy simulation | `dag-validation.md` | A5-20 |
| V2-FR-018 idempotency | `per-node-governance.md` | A5-15 |
| V2-FR-019 replay simulation | `checkpoint-and-resume.md` | A5-21 |
| V2-FR-024 lease/heartbeat/dead-letter | `checkpoint-and-resume.md` | A5-16, A5-12 |
| V2-SEC-003 per-node policy evaluation | `per-node-governance.md` | A5-09 |
| V2-SEC-005 approval bound to immutable digest | `plan-digest.md` | A5-07, A5-08 |
| V2-SEC-011 atomic single-use approval, anti-replay | `plan-digest.md` | A5-08 |
| V2-SEC-012 no arbitrary code execution | `plan-definition.md`, `dag-validation.md` | A5-03, A5-04 |
| V2-SEC-019 provenance auditable by version/digest | `per-node-governance.md` | A5-14, A5-22 |

## ADR reconciliation

| ADR | Status | Phase 5 effect |
|---|---|---|
| ADR-0003 typed tools, no generic shell | Accepted | Hard constraint; A5-03 |
| ADR-0008 batch semantics | Proposed | Consumed as scheduler substrate |
| ADR-0009 DAG semantics | Proposed | This lane is its detail; open questions narrowed to OD-003/OD-024 |
| ADR-0011 per-node policy | Proposed | Applied unchanged; plan cannot widen a node |
| ADR-0012 approval ↔ plan digest | Proposed | Specialized; composition rule with `operation_digest` added |
| ADR-0013 idempotency/replay | Proposed | Node key derived from `plan_digest` |
| ADR-0014 saga/compensation | Proposed | Reverse-topological, read-back-verified, unsafe⇒dead-letter |
| ADR-0021 operation digest (single node) | Proposed | Composed under `plan_digest` |
| ADR-0022 GitHub idempotency/concurrency | Proposed | Reused; resource-lock serialization added |

No new ADR is proposed by this lane yet. Candidates for Phase 5 ADRs, to be
raised when the lane moves from design to implementation: canonical plan
serialization (OD-018), durable store choice (OD-003), transform DSL closure
(OD-024), replay format (OD-021).

## Open decisions that must be CLOSED before `DAG_ACCEPTED`

| OD | Why it blocks |
|---|---|
| OD-003 durable queue/store | Determines lease/fencing and checkpoint atomicity guarantees |
| OD-018 canonical serialization | Determines `plan_digest` byte-level determinism and approval compatibility |
| OD-021 replay format | Determines A5-21 semantics |
| OD-024 transform DSL | Determines A5-04's closed set |

Related but not blocking: OD-006 (concurrency defaults, owned by Phase 4),
OD-007 (principal model), OD-008 (approval UX), OD-011 (retry defaults).

## Explicitly deferred out of Phase 5

Runbook registry/signing (Phase 6), non-GitHub integrations (Phase 7), agentic
escalation and hybrid fallback (Phase 8), SLOs, chaos and production hardening
(Phase 9), cost-aware planning (OD-023), dynamic capability discovery
(OD-012/013).
