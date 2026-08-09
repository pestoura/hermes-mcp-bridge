# V2 Downstream Design Lane — Phases 7, 8, 9

>
> **V2 · DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE UNTIL PREDECESSORS ACCEPTED**
>
> This subtree is design-only. No runtime file, gate, tool surface or policy path
> is changed by it. Predecessor gates `DIRECT_MUTATION_ACCEPTED` (Phase 3),
> `BATCH_ACCEPTED` (Phase 4), `DAG_ACCEPTED` (Phase 5) and `RUNBOOK_ACCEPTED`
> (Phase 6) are **not** accepted at the time of writing. The operational V1
> surface remains exactly **27 tools**, bridge `1.0.0`, schema `0.6.1`.

## Purpose

Prepare, in parallel with Phase 3–6 delivery, the design that Phases 7 (Additional
Integrations), 8 (HYBRID Execution) and 9 (Production Hardening) require, so that
each phase can start immediately after its predecessor gate without inventing
security, economics or reliability semantics under delivery pressure.

## Boundary rules for this lane

1. Only files under `docs/v2/downstream/` are added or changed.
2. No Phase 3–6 implementation file (`src/`, `tests/`, `scripts/`) is touched.
3. Controller-owned documents are **not** edited here: `docs/v2/roadmap.md`,
   `docs/v2/requirements/traceability-matrix.md`, `docs/v2/evidence/README.md`.
   Required roadmap/traceability rows are *proposed* in
   `acceptance-matrix.md` for the Controller to apply at promotion time.
4. New ADRs are proposed as text inside this lane (`adr-proposals.md`) and are
   only promoted into `docs/v2/adrs/` by the Controller when the owning phase
   opens.
5. Nothing here may be cited as acceptance evidence.

## Contents

| Lane | Scope |
|---|---|
| `phase7/` | Integration plugin boundary, Tool/Capability contracts, capability discovery, credential isolation, audit and fail-closed policy |
| `phase8/` | Deterministic resolver decision tree, thresholds, reason codes, token/cost/latency evidence, safety non-downgrade |
| `phase9/` | Performance/latency targets, failure injection, chaos/recovery, audit completeness, bounded-label observability, secret scanning, supply chain/SBOM, rollback drills, production acceptance |
| `acceptance-matrix.md` | Consolidated gate/criterion/evidence matrix and proposed traceability rows |
| `adr-proposals.md` | ADR-0024..ADR-0031 proposals (text only, not promoted) |
| `dependencies.md` | Predecessor dependency and merge-order contract |

## Reconciliation with the live repository

Reconciled against `main` at commit `690f004` (Phase 3 lane L5 merged; Phases
0–2 accepted). This lane does not restate accepted decisions; it refines them:

| Existing document | Relationship |
|---|---|
| `../architecture/target-architecture.md` | Base component model; Phase 7 adds the provider plugin boundary |
| `../architecture/capability-projection.md` | Base projection; Phase 7 adds direct-read/direct-write discovery classes |
| `../architecture/credential-broker.md` | Base broker contract; Phase 7 adds per-provider isolation domains |
| `../architecture/execution-modes.md` | Base mode set; Phase 8 makes selection deterministic and evidenced |
| `../architecture/observability-token-economics.md` | Base metrics/economics; Phase 9 bounds label cardinality and completes audit |
| `../phase3/*` | Mutation semantics reused verbatim by Phase 7 direct-write providers |
| `../security/threat-model.md` | Base threat set; each lane adds only its delta |
| `../open-decisions.md` | OD-005, OD-007, OD-011..OD-014, OD-016, OD-017 remain open and are treated as blocking inputs, not assumptions |
