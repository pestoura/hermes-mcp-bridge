# Downstream Acceptance Matrix (Phases 7–9)

>
> **V2 · DOWNSTREAM DESIGN ONLY · DO_NOT_MERGE UNTIL PREDECESSORS ACCEPTED**
>
> This matrix is a *proposal*. The Controller owns `../roadmap.md` and
> `../requirements/traceability-matrix.md`; the rows below are staged here for
> the Controller to apply when each phase opens. Nothing here is acceptance.

## Gate summary

| Phase | Gate | Blocking predecessor | Design status | Implementation |
|---|---|---|---|---|
| 7 | `INTEGRATION_<X>_ACCEPTED` (per provider) | `RUNBOOK_ACCEPTED` + OD-005, OD-007 | DESIGN_READY | NOT_STARTED |
| 8 | `HYBRID_ACCEPTED` | Phases 3–6 + ≥2 accepted integrations | DESIGN_READY | NOT_STARTED |
| 9 | `V2_PRODUCTION_READY` | `HYBRID_ACCEPTED` + production-cut integrations | DESIGN_READY | NOT_STARTED |

## Phase 7 — per-integration criteria

| # | Criterion | Evidence artifact | Fail-closed default |
|---|---|---|---|
| 7.1 | Predecessor gates declared by SHA | evidence manifest | block |
| 7.2 | Manifest validation (tools, scopes, egress) | load-time report | refuse load |
| 7.3 | Dedicated least-privilege credential, scope-set digest | credential report, `broad_credential=false` | DENY |
| 7.4 | Health probe; no write capability in `DEGRADED` | readiness snapshot | `UNAVAILABLE` |
| 7.5 | Fail-closed ordering: 0 provider calls / 0 credential resolutions on denial | hermetic test output | DENY |
| 7.6 | Test matrix `P7-01..P7-20`, zero failures | test report | block |
| 7.7 | Audit completeness 100%, refusals included | reconciliation report | block |
| 7.8 | Redaction scan zero findings | scan report | block |
| 7.9 | Snapshot + `write_capability_digest` deterministic across 2 runs | digest pair | block |
| 7.10 | V1 = 27 tools, no V1→V2 import | regression evidence | block |
| 7.11 | Provider disable-by-allow-list verified | rollback run | block |
| 7.12 | Connected sanitized evidence; DIRECT tokens = 0; residual mutations 0 | connected evidence + manifest | block |

## Phase 8 — `HYBRID_ACCEPTED`

| # | Criterion | Evidence artifact | Fail-closed default |
|---|---|---|---|
| 8.1 | Predecessors + ≥2 integrations accepted | evidence manifest | block |
| 8.2 | Decision tree S0–S8 mapped 1:1 to code paths | design/code mapping | block |
| 8.3 | Determinism: 100 replays/scenario, 0 mismatches | replay report | `E-RESOLVER-NONDETERMINISM` |
| 8.4 | Closed reason-code enumeration; unknown codes = 0 | outcome census | block |
| 8.5 | Zero-default agentic (no implicit escalation) | negative tests | `E-AGENTIC-NOT-ALLOWED` |
| 8.6 | Invariants I1–I10 each covered; `P8-01..P8-20` pass | test report | block |
| 8.7 | Matched-baseline economics: absolute tokens, latency p50/p95, coverage | economics record | block |
| 8.8 | Escalations ≤ `MAX_ESCALATIONS_PER_REQUEST` | run census | `E-AGENTIC-BUDGET-EXHAUSTED` |
| 8.9 | Audit completeness 100% across all modes | reconciliation report | block |
| 8.10 | Redaction scan of decision records/labels: zero findings | scan report | block |
| 8.11 | HYBRID disable returns prior accepted behaviour | rollback run | block |
| 8.12 | V1 = 27 tools | regression evidence | block |

## Phase 9 — `V2_PRODUCTION_READY`

| # | Criterion | Evidence artifact | Fail-closed default |
|---|---|---|---|
| 9.1 | Phase 8 + all production-cut integrations accepted | evidence manifest | block |
| 9.2 | Performance targets with measured distributions + sample counts | perf report | block |
| 9.3 | `F-01..F-20` fail-closed; duplicate mutations 0 | injection report | block |
| 9.4 | `C-01..C-08`; RTO ≤ 5 min; audit RPO 0; unknowns surfaced | chaos report | block |
| 9.5 | Audit completeness 100% (independent reconciliation) + digest chain verified | audit report | block |
| 9.6 | Bounded labels; adversarial cardinality test; ceiling respected | observability report | block |
| 9.7 | Secret scan of tree, history window, artifacts; `scanned=false` fails | scan report + scanner version | block |
| 9.8 | SBOM, pinned+hashed deps, vuln scan, provenance, immutable digests | SBOM + scan + provenance | block |
| 9.9 | Rollback drill ≤ 15 min; rotation drill per domain; restore verified | drill logs | block |
| 9.10 | Approval/idempotency replay protection proven | replay tests | block |
| 9.11 | V1 preserved: 27 tools, versions recorded | regression evidence | block |
| 9.12 | Operational runbooks complete | docs index | block |

## Proposed traceability rows (Controller applies; not edited here)

| Requirement(s) | ADR (proposed) | Component | Phase | Test/evidence | Gate |
|---|---|---|---|---|---|
| V2-FR-014, V2-FR-016, V2-SEC-004/005 | ADR-0024, ADR-0026 | Provider Plugin Boundary / Credential Domains | 7 | `P7-01..P7-20`, connected per-provider evidence | `INTEGRATION_<X>_ACCEPTED` |
| V2-FR-013, V2-FR-012 | ADR-0025 | Capability Discovery (direct-read/direct-write) | 7 | snapshot + `write_capability_digest` determinism | `INTEGRATION_<X>_ACCEPTED` |
| V2-SEC-011/021/025 | ADR-0030 | Integration Audit / Fail-closed Policy | 7 | audit completeness + redaction scan | `INTEGRATION_<X>_ACCEPTED` |
| V2-FR-007 | ADR-0027, ADR-0028 | Deterministic Resolver / Escalation | 8 | `P8-01..P8-20`, replay determinism | `HYBRID_ACCEPTED` |
| V2-NFR-001/002, V2-FR-008 | ADR-0018 (existing) | Token/Cost/Latency Evidence | 8 | matched-baseline economics record | `HYBRID_ACCEPTED` |
| V2-NFR-003..013 | ADR-0029 | Observability / SLOs | 9 | perf + observability reports | `V2_PRODUCTION_READY` |
| V2-NFR-015..020, V2-SEC-014..020 | ADR-0031 | Release Integrity / Resilience | 9 | injection, chaos, SBOM, scans, drills | `V2_PRODUCTION_READY` |

Requirement ids above are the intended mapping targets from
`../requirements/`; the Controller confirms exact ids when applying the rows.
