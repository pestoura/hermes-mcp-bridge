# V2 Traceability Matrix

> **V2 · IMPLEMENTATION IN PROGRESS · PHASES 0–1 ACCEPTED · PHASE 2 CORE IMPLEMENTED · NO IMPACT ON V1**

Entries may reference either planned tests for future phases or implemented
tests/evidence for completed/in-progress phases. A gate is satisfied only by its
own retained evidence; a test name in this matrix is not itself an acceptance.

| Requirement(s) | ADR / design | Component | Phase | Test/evidence | Gate |
|---|---|---|---|---|---|
| V2-FR-001, V2-NFR-001 | ADR-0002/0003 | GitHub DIRECT Executor | 2 | `tests/test_v2_phase2_github_direct.py`: exact five DIRECT reads, no Hermes client/prompt/agent path, fixed GitHub GET endpoints; connected zero-token/shadow evidence still pending | DIRECT_READ_ACCEPTED |
| V2-FR-002..003, V2-NFR-002 | ADR-0008 | Batch Scheduler | 4 | `test_batch_parallel_independent`; bounded worker evidence | BATCH_ACCEPTED |
| V2-FR-004..005, V2-FR-019 | ADR-0009 | DAG Scheduler | 5 | `test_dag_dependencies_and_typed_bindings` | DAG_ACCEPTED |
| V2-FR-006, V2-FR-015, V2-SEC-010 | ADR-0010 | Runbook Registry | 6 | `test_runbook_version_digest_compile` | RUNBOOK_ACCEPTED |
| V2-FR-007 | ADR-0016 | Agentic Escalation | 8 | `test_hybrid_explicit_escalation_budget` | HYBRID_ACCEPTED |
| V2-FR-008, V2-NFR-014 | ADR-0015 | Result Shaper / Artifact Store | 2/4/5 | Phase 2: endpoint allow-lists, explicit field selection and canonical byte budget in `test_v2_phase2_github_direct.py`; later artifact-store tests | DIRECT_READ_ACCEPTED / BATCH_ACCEPTED / DAG_ACCEPTED |
| V2-FR-009, V2-FR-024 | ADR-0009 | Checkpoint/Lease Store | 5 | `test_resume_after_lease_recovery` | DAG_ACCEPTED |
| V2-FR-010..011 | ADR-0008/0009 | Result Aggregator | 4/5 | `test_partial_success_explicit` | BATCH_ACCEPTED / DAG_ACCEPTED |
| V2-FR-012 | ADR-0004 | Tool Registry | 1 | `tests/test_v2_phase1_registry_core.py`; canonical schema/invariant evidence in `v2_phase1_registry_acceptance.py` | REGISTRY_ACCEPTED |
| V2-FR-013 | ADR-0005 | Capability Projection | 1 | authorized-only projection, exact projected field allow-list and excluded reason-code evidence | REGISTRY_ACCEPTED |
| V2-FR-014, V2-SEC-002/006 | ADR-0006/0007 | Credential Broker / GitHub Authorization | 1/2 | Phase 1: capability-ID/status-only contract; Phase 2 core: redacted `GitHubAuthorizationProvider`, readiness-before-resolution and zero-resolution-on-deny tests; real least-privilege provider evidence pending | REGISTRY_ACCEPTED / DIRECT_READ_ACCEPTED |
| V2-FR-016, V2-SEC-012 | ADR-0009 | Transform Engine | 5 | `test_transform_no_eval_or_code` | DAG_ACCEPTED |
| V2-FR-017 | ADR-0011 | Policy Engine | 1/2 | Phase 1 policy acceptance; Phase 2 scope→policy/readiness→authorization ordering and explicit per-tool rules in `test_v2_phase2_github_direct.py` | REGISTRY_ACCEPTED / DIRECT_READ_ACCEPTED |
| V2-FR-018, V2-SEC-004 | ADR-0013 | Idempotency Store | 3 | duplicate mutation/replay tests | DIRECT_MUTATION_ACCEPTED |
| V2-FR-020, V2-SEC-023 | ADR-0015/0018 | Evidence Layer | 4/9 | manifest/artifact integrity tests | V2_PRODUCTION_READY |
| V2-FR-021 | ADR-0004 | Capability Health | 1 | exact seven-state configured/available/healthy/ready conformance matrix | REGISTRY_ACCEPTED |
| V2-FR-022 | ADR-0015 | Replay Simulation | 5/9 | simulation does not call mutation backends | V2_PRODUCTION_READY |
| V2-FR-023, V2-NFR-019 | ADR-0008 | Scheduler | 2/4 | Phase 2 stable 403/429/Retry-After classification without retry; later circuit-breaker/adaptive-concurrency tests | DIRECT_READ_ACCEPTED / BATCH_ACCEPTED |
| V2-FR-025, V2-NFR-009/020 | ADR-0017 | Compatibility Layer | all | V1 27-tool regression in Phase 1/2; connected DIRECT-vs-V1 shadow comparison still pending | each affected gate / V2_PRODUCTION_READY |
| V2-SEC-001/003/009/020 | ADR-0011 | Policy Engine | 1+ | Phase 1 fail-closed suite; Phase 2 exact repository scope before readiness, policy denial, missing material and zero-network negative tests | each affected gate |
| V2-SEC-005/011 | ADR-0012 | Approval Store | 3/5 | digest mismatch/replay/TOCTOU tests | DIRECT_MUTATION_ACCEPTED |
| V2-SEC-007/015..018 | ADR-0003/0019 | Execution Sandbox | 2/3/7 | Phase 2 fixed `api.github.com`, GET-only, no redirects, `trust_env=false`, search qualifier-injection negative tests; broader path/service/egress/socket tests later | DIRECT_READ_ACCEPTED / V2_PRODUCTION_READY |
| V2-SEC-013/014/019 | ADR-0004/0005 | Registry/Projection/Schema | 1 | no backend pass-through, materialized sensitive-schema rejection, editorial-text exclusion, snapshot/projection digests | REGISTRY_ACCEPTED |
| V2-SEC-021 | ADR-0014 | Saga/Compensation | 5/6 | unsafe compensation rejection | DAG_ACCEPTED |
| V2-SEC-022, V2-NFR-008/011 | ADR-0008 | Budgets/Scheduler | 2/4 | Phase 2 shaped-result byte budget; later fan-out budget/backpressure tests | DIRECT_READ_ACCEPTED / BATCH_ACCEPTED |
| V2-SEC-024, V2-NFR-003..005/015 | ADR-0018 | Observability | 0/2/9 | Phase 2 raw-vs-returned byte counters in result contract; connected latency/API-call/zero-token evidence pending; later telemetry cardinality/secret scan | DIRECT_READ_ACCEPTED / V2_PRODUCTION_READY |
| V2-NFR-013, V2-NFR-017 | ADR-0004 | Capability Snapshot / Registry Schema | 1 | canonical byte determinism, material-change hash test, explicit `v2.phase1.1` snapshot schema version | REGISTRY_ACCEPTED |

## Phase 1 acceptance chain

```text
Phase 1 requirements above
        ↓
core tests (tests/test_v2_phase1_registry_core.py)
        ↓
collector (scripts/v2_phase1_registry_acceptance.py)
        ↓
fail-closed validator (scripts/validate_v2_phase1_registry_evidence.py)
        ↓
blocking draft release bound to the exact tested commit
        ↓
revalidated repository evidence after integrated-main validation
        ↓
REGISTRY_ACCEPTED
```

## Phase 2 current chain

```text
accepted Phase 1 registry/policy/credential contract
        ↓
GitHub DIRECT typed core (github_registry.py / github_direct.py)
        ↓
hermetic security + result-shaping tests
        ↓
V1 regression + Phase 1 gate remain GREEN
        ↓
github.read provider adapter + readiness broker       [IMPLEMENTED]
        ↓
DIRECT canary router, default OFF, exact allow-list   [IMPLEMENTED]
        ↓
sanitized provider attestation with live probes       [IMPLEMENTED]
        ↓
connected collector (5x3, fail-closed)                [IMPLEMENTED]
        ↓
least-privilege GitHub App / fine-grained credential  [SATISFIED]
        ↓
DIRECT vs V1 shadow evidence + zero Hermes LLM tokens [MEASURED]
        ↓
OUTER out-of-band integrity/provenance gate           [ACCEPTED]
        ↓
retained connected + OOB evidence                     [RETAINED]
        ↓
DIRECT_READ_ACCEPTED                                  [DECLARED]
        ↓
OUTER overall_status                                  [ACCEPTED]
```

Repo-side status: **PHASE 2 ACCEPTED.** The connected run used a
least-privilege GitHub App installation credential (`broad_pat=false`), not the
classic broad PAT that this gate refuses. Accepted source commit
`818c56a467ed00b1412a219c78e3c68007848df3`: 15/15 semantic matches, 15/15
provenance pass, `direct_total_tokens=0` against `agentic_total_tokens=13784`
(100% reduction), 15 DIRECT provider calls, zero upstream Hermes LLM calls,
zero mutations, and an out-of-band real-state delta of exactly zero rows with an
identical before/after fingerprint. Retained evidence and digests are indexed in
`docs/v2/evidence/README.md`; the gate conditions remain authoritative in
`docs/v2/phase2-connected-acceptance.md` and
`docs/v2/phase2-final-outer-gate.md`.

Deferred design decisions such as a real generalized credential backend
(OD-005), principal/tenant authorization (OD-007) and dynamic
registry/projection discovery (OD-012/OD-013) are not silently treated as
Phase 1/2 acceptance evidence. The dedicated Phase 2 `github.read` provider can
be implemented narrowly after connected discovery without closing the future
generalized backend decision.
