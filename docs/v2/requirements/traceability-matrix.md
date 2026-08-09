# V2 Traceability Matrix

> **V2 · IMPLEMENTATION IN PROGRESS · PHASES 0–5 ACCEPTED · NO IMPACT ON V1**

Entries may reference either planned tests for future phases or implemented
tests/evidence for completed/in-progress phases. A gate is satisfied only by its
own retained evidence; a test name in this matrix is not itself an acceptance.

| Requirement(s) | ADR / design | Component | Phase | Test/evidence | Gate |
|---|---|---|---|---|---|
| V2-FR-001, V2-NFR-001 | ADR-0002/0003 | GitHub DIRECT Executor | 2 | `tests/test_v2_phase2_github_direct.py`: exact five DIRECT reads, no Hermes client/prompt/agent path, fixed GitHub GET endpoints; connected zero-token/shadow evidence still pending | DIRECT_READ_ACCEPTED |
| V2-FR-002..003, V2-NFR-002 | ADR-0008 | Batch Scheduler | 4 | `tests/test_v2_phase4_batch_scheduler.py` S-01..S-27 executed for real; live `max_observed_inflight=2`; evidence `docs/v2/evidence/phase4-batch-acceptance.json` (`failures=[]`) | BATCH_ACCEPTED **[ACCEPTED]** |
| V2-FR-004..005, V2-FR-019 | ADR-0009/0025 | DAG Scheduler | 5 | `test_a5_05*` explicit-edge/cycle/self-dep/unreachable rejection, `test_a5_06*` typed bindings, `test_a5_10*` bounded deterministic scheduling and same-resource serialization; evidence `docs/v2/evidence/phase5-dag-acceptance.json` | DAG_ACCEPTED **[ACCEPTED]** |
| V2-FR-006, V2-FR-015, V2-SEC-010 | ADR-0028/0029 | Runbook Registry | 6 | `test_a6_05` append-only `(runbook_id, version)` with refused re-admission, `test_a6_08` yanked runbooks non-invocable, `test_a6_22` tamper detection and clean redaction scan, `test_a6_23` unauthorized callers get `RB_UNKNOWN`; evidence `docs/v2/evidence/phase6-runbook-acceptance.json` | RUNBOOK_ACCEPTED **[ACCEPTED]** |
| V2-FR-026, V2-SEC-025 | ADR-0028 | Runbook Admission / Canonical IR | 6 | A6-03 byte-identical IR and stable `runbook_digest` under editorial edits, A6-04 zero-I/O admission, A6-06 no floating pins, A6-09 closed parameter schema, A6-10 no templating/expression bindings, A6-11 exact capability match in both directions | RUNBOOK_ACCEPTED **[ACCEPTED]** |
| V2-FR-027, V2-SEC-026 | ADR-0029 | Runbook Invocation / Approval Binding | 6 | A6-07 mandatory `expected_runbook_digest`, A6-12..A6-16 policy/destructive/rollback/timeout fail-closed, A6-17 agentic budget defaults and required 0 before HYBRID, A6-18 single-use bound approval, A6-19 idempotency, A6-20 write-ahead evidence before every mutation, A6-21 owner and review cadence, A6-24/A6-25 exemplar and `plan_digest` equivalence | RUNBOOK_ACCEPTED **[ACCEPTED]** |
| V2-FR-007 | ADR-0016 | Agentic Escalation | 8 | `test_hybrid_explicit_escalation_budget` | HYBRID_ACCEPTED |
| V2-FR-008, V2-NFR-014 | ADR-0015 | Result Shaper / Artifact Store | 2/4/5 | Phase 2: endpoint allow-lists, explicit field selection and canonical byte budget in `test_v2_phase2_github_direct.py`; later artifact-store tests | DIRECT_READ_ACCEPTED / BATCH_ACCEPTED / DAG_ACCEPTED |
| V2-FR-009, V2-FR-024 | ADR-0009/0024 | Checkpoint/Lease Store | 5 | `test_a5_15` write-ahead before mutation, `test_a5_16*` resume reconciliation, `test_a5_18` stale fence token refused, `test_a5_19*` tampered/unsupported state refused (`v2/dag_store.py`, SQLite WAL) | DAG_ACCEPTED **[ACCEPTED]** |
| V2-FR-010..011 | ADR-0008/0009 | Result Aggregator | 4/5 | Phase 4 partial-success suite; Phase 5 `plan_status_from` precedence plus `test_a5_12*` (INDETERMINATE dominates, `unknown_effects` cannot be silently dropped) | BATCH_ACCEPTED / DAG_ACCEPTED **[BOTH ACCEPTED]** |
| V2-FR-012 | ADR-0004 | Tool Registry | 1 | `tests/test_v2_phase1_registry_core.py`; canonical schema/invariant evidence in `v2_phase1_registry_acceptance.py` | REGISTRY_ACCEPTED |
| V2-FR-013 | ADR-0005 | Capability Projection | 1 | authorized-only projection, exact projected field allow-list and excluded reason-code evidence | REGISTRY_ACCEPTED |
| V2-FR-014, V2-SEC-002/006 | ADR-0006/0007 | Credential Broker / GitHub Authorization | 1/2 | Phase 1: capability-ID/status-only contract; Phase 2 core: redacted `GitHubAuthorizationProvider`, readiness-before-resolution and zero-resolution-on-deny tests; real least-privilege provider evidence pending | REGISTRY_ACCEPTED / DIRECT_READ_ACCEPTED |
| V2-FR-016, V2-SEC-012 | ADR-0026 | Transform Engine | 5 | `test_a5_04` closed 12-operation set with typed arity and output-size bound; `test_a5_03` AST scan proves no `eval`/`exec`/`compile`/shell/socket/HTTP in any Phase 5 module | DAG_ACCEPTED **[ACCEPTED]** |
| V2-FR-017 | ADR-0011 | Policy Engine | 1/2 | Phase 1 policy acceptance; Phase 2 scope→policy/readiness→authorization ordering and explicit per-tool rules in `test_v2_phase2_github_direct.py` | REGISTRY_ACCEPTED / DIRECT_READ_ACCEPTED |
| V2-FR-018, V2-SEC-004 | ADR-0013/0022 | Idempotency Store | 3/5 | Phase 3 accepted at `8fc8363a…` (`failures=[]`); Phase 5 adds per-node `node_idempotency_key` written ahead of any provider call (`test_a5_15`) and never re-issued on resume when the effect is proven (`test_a5_16`) | DIRECT_MUTATION_ACCEPTED **[ACCEPTED]** / DAG_ACCEPTED **[ACCEPTED]** |
| V2-FR-020, V2-SEC-023 | ADR-0015/0018 | Evidence Layer | 4/9 | manifest/artifact integrity tests | V2_PRODUCTION_READY |
| V2-FR-021 | ADR-0004 | Capability Health | 1 | exact seven-state configured/available/healthy/ready conformance matrix | REGISTRY_ACCEPTED |
| V2-FR-022 | ADR-0015/0027 | Replay Simulation | 5/9 | `test_a5_20` zero external calls, `replay=true` durable on the checkpoint and report; `test_a5_20b` replay consumes no approval. Connected production-scale replay remains Phase 9 | DAG_ACCEPTED **[ACCEPTED]** / V2_PRODUCTION_READY |
| V2-FR-023, V2-NFR-019 | ADR-0008 | Scheduler | 2/4 | Phase 2 stable 403/429/Retry-After classification without retry; later circuit-breaker/adaptive-concurrency tests | DIRECT_READ_ACCEPTED / BATCH_ACCEPTED |
| V2-FR-025, V2-NFR-009/020 | ADR-0017 | Compatibility Layer | all | V1 27-tool regression in Phase 1/2; connected DIRECT-vs-V1 shadow comparison still pending | each affected gate / V2_PRODUCTION_READY |
| V2-SEC-001/003/009/020 | ADR-0011 | Policy Engine | 1+ | Phase 1 fail-closed suite; Phase 2 exact repository scope before readiness, policy denial, missing material and zero-network negative tests | each affected gate |
| V2-SEC-005/011 | ADR-0012/0021/0025 | Approval Store | 3/5 | Phase 3 accepted; Phase 5 `test_a5_08*` covers digest mismatch, expiry, insufficient scope, atomic single-use nonce and uncovered `operation_digest`; `test_a5_17` proves a resume re-evaluates policy without re-consuming the approval | DIRECT_MUTATION_ACCEPTED **[ACCEPTED]** / DAG_ACCEPTED **[ACCEPTED]** |
| V2-SEC-007/015..018 | ADR-0003/0019 | Execution Sandbox | 2/3/7 | Phase 2 fixed `api.github.com`, GET-only, no redirects, `trust_env=false`, search qualifier-injection negative tests; broader path/service/egress/socket tests later | DIRECT_READ_ACCEPTED / V2_PRODUCTION_READY |
| V2-SEC-013/014/019 | ADR-0004/0005 | Registry/Projection/Schema | 1 | no backend pass-through, materialized sensitive-schema rejection, editorial-text exclusion, snapshot/projection digests | REGISTRY_ACCEPTED |
| V2-SEC-021 | ADR-0014 | Saga/Compensation | 3/5/6 | `test_a5_11` reverse-topological compensation with read-back verification, `test_a5_11b` unsafe compensation writes nothing and is reported, `test_a5_11c` compensation is policy-evaluated; INDETERMINATE is never compensated (`test_a5_12`) | DIRECT_MUTATION_ACCEPTED **[ACCEPTED]** / DAG_ACCEPTED **[ACCEPTED]** |
| V2-SEC-022, V2-NFR-008/011 | ADR-0008/0009 | Budgets/Scheduler | 2/4/5 | Phase 2 shaped-result byte budget; Phase 4 bounded worker pool; Phase 5 `test_a5_22*` depth/fan-out/external-call ceilings, mutating plans cannot declare parallelism, budget exhaustion skips rather than overruns | DIRECT_READ_ACCEPTED / BATCH_ACCEPTED / DAG_ACCEPTED **[ACCEPTED]** |
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

## Phase 4 acceptance chain

```text
accepted Phase 3 mutation contract (DIRECT_MUTATION_ACCEPTED, failures=[])
        ↓
S-01..S-27 acceptance suite (tests/test_v2_phase4_batch_scheduler.py)
        ↓
executable gate (scripts/validate_v2_phase4_batch_gate.py)
   INNER: V1 contract · limits · flag off · real suite run · live concurrency
   OUTER: module SHA-256 binding · AST generic-surface scan · Phase 3 marker
        ↓
docs/v2/evidence/phase4-batch-acceptance.json  (failures=[])
        ↓
BATCH_ACCEPTED
```

## Phase 5 acceptance chain

```text
accepted Phase 4 batch contract (BATCH_ACCEPTED, failures=[])
        ↓
design lane docs/v2/phase5/ + frozen fixture corpus tests/fixtures/v2_phase5/
        ↓
A5-01..A5-22 acceptance suite (tests/test_v2_phase5_dag_acceptance.py)
        ↓
executable gate (scripts/validate_v2_phase5_dag_gate.py)
   INNER: V1 contract · limits · flag off · closed transform set ·
          real suite run · live determinism probe · live durability probe
   OUTER: module SHA-256 binding · AST generic-surface scan ·
          no DAG tool in the V1 projection · Phase 4 marker
        ↓
docs/v2/evidence/phase5-dag-acceptance.json  (failures=[])
        ↓
DAG_ACCEPTED
```

Phase 5 closes OD-003 (ADR-0024), OD-018 for plans (ADR-0025), OD-024
(ADR-0026) and OD-021 (ADR-0027). The DAG runtime ships behind
`DAG_FEATURE_ENABLED = False` and is not wired to MCP; the V1 contract remains
`1.0.0` / `0.6.1` / 27 tools throughout.

## Phase 6 acceptance chain

```text
accepted Phase 5 DAG engine (DAG_ACCEPTED, failures=[])
        ↓
design lane docs/v2/phase6/ + ADR-0028..ADR-0031 + exemplar fixture
tests/fixtures/v2_phase6/RB-GITHUB-PR-LIFECYCLE-001.json
        ↓
A6-01..A6-26 acceptance suite (tests/test_v2_phase6_runbook_acceptance.py)
        ↓
executable gate (scripts/validate_v2_phase6_runbook_gate.py)
   INNER: V1 contract · flag off · agentic budget 0 · real suite run ·
          live IR/digest determinism probe · live append-only registry probe ·
          live unauthorized-caller RB_UNKNOWN probe
   OUTER: module SHA-256 binding · AST generic-surface scan ·
          design lane + ADR set · traceability matrix · Phase 5 marker
        ↓
docs/v2/evidence/phase6-runbook-acceptance.json  (failures=[])
        ↓
RUNBOOK_ACCEPTED
```

### Criterion → implementing test

| Criterion | Implementing test | Proves |
|---|---|---|
| A6-01 | `test_a6_01_prior_gates_declared_before_phase6` | prior gates declared before Phase 6 |
| A6-02 | `test_a6_02_v1_contract_unchanged` | contract 1.0.0 / schema 0.6.1 / exactly 27 tools |
| A6-03 | `test_a6_03_admission_is_deterministic` | byte-identical IR; editorial edits do not move the digest |
| A6-04 | `test_a6_04_admission_performs_no_io` | admission opens no socket and resolves no credential |
| A6-05 | `test_a6_05_registry_is_append_only` | `(runbook_id, version)` immutable, conflict refused |
| A6-06 | `test_a6_06_unpinned_references_rejected` | no empty/`*`/`latest` pins |
| A6-07 | `test_a6_07_digest_mismatch_denies_without_side_effects` | mandatory `expected_runbook_digest` |
| A6-08 | `test_a6_08_yanked_runbook_is_not_invocable` | yanked is immediately non-invocable |
| A6-09 | `test_a6_09_parameter_schema_is_closed`, `test_a6_09b_oversize_and_out_of_constraint_arguments_denied` | closed schema, no secret-shaped parameters |
| A6-10 | `test_a6_10_unsafe_bindings_rejected` | no templating/expression/env/file bindings |
| A6-11 | `test_a6_11_capability_match_is_exact_both_directions`, `test_a6_11b_administrative_capability_forbidden` | superset and subset both fail |
| A6-12 | `test_a6_12_weaker_policy_class_rejected` | declared class ≥ computed aggregate |
| A6-13 | `test_a6_13_destructive_underdeclaration_rejected`, `test_a6_13b_destructive_forces_dual_approval` | destructive computed and compared |
| A6-14 | `test_a6_14_automatic_rollback_requires_registered_compensation`, `test_a6_14b_not_supported_rollback_requires_accepted_irreversibility` | rollback declared per mutating node |
| A6-15 | `test_a6_15_unsafe_compensation_does_not_write` | unsafe inverse writes nothing |
| A6-16 | `test_a6_16_timeouts_bounded_and_consistent` | timeouts present, bounded, consistent |
| A6-17 | `test_a6_17_agentic_budget_defaults_to_zero_and_cannot_be_widened`, `test_a6_17b_caller_cannot_widen_budget` | agentic budget 0; caller may only tighten |
| A6-18 | `test_a6_18_approval_is_bound_and_single_use` | approval bound to `plan_digest`, single use |
| A6-19 | `test_a6_19_execution_idempotency` | replay adds zero provider mutations |
| A6-20 | `test_a6_20_write_ahead_record_precedes_every_mutation` | write-ahead evidence before every mutation |
| A6-21 | `test_a6_21_owner_and_review_cadence_required`, `test_a6_21b_overdue_review_denies_high_blast_invocation` | resolvable owner, bounded cadence |
| A6-22 | `test_a6_22_registry_records_carry_no_secret_material` | integrity protected, tamper detectable, redaction clean |
| A6-23 | `test_a6_23_unauthorized_caller_receives_rb_unknown` | authorized-only projection, `RB_UNKNOWN` |
| A6-24 | `test_a6_24_exemplar_runbook_admits_promotes_and_executes` | exemplar admitted, promoted, executed |
| A6-25 | `test_a6_25_migration_equivalence_runbook_vs_reference_dag` | identical `plan_digest` runbook ↔ reference DAG |
| A6-26 | `test_a6_26_every_criterion_traces_to_a_test_and_a_requirement`, `test_a6_26b_feature_flag_defaults_off_and_engine_is_fail_closed` | full traceability, flag off, fail-closed engine |

Phase 6 closes OD-002 (ADR-0028), OD-018 for runbooks (ADR-0028), OD-019
(ADR-0029), least privilege by computed capability (ADR-0030) and the computed
destructive marker (ADR-0031). The runbook runtime ships behind
`RUNBOOK_FEATURE_ENABLED = False` and is not wired to MCP; the V1 contract
remains `1.0.0` / `0.6.1` / 27 tools throughout.
