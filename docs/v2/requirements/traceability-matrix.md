# V2 Traceability Matrix

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

Tests are planned names, not implemented tests.

| Requirement(s) | ADR / design | Component | Phase | Planned test/evidence | Gate |
|---|---|---|---|---|---|
| V2-FR-001, V2-NFR-001 | ADR-0002 | Direct Executor | 2 | `test_direct_read_no_agentic_invocation` | DIRECT_READ_ACCEPTED |
| V2-FR-002..003, V2-NFR-002 | ADR-0008 | Batch Scheduler | 4 | `test_batch_parallel_independent`; bounded worker evidence | BATCH_ACCEPTED |
| V2-FR-004..005, V2-FR-019 | ADR-0009 | DAG Scheduler | 5 | `test_dag_dependencies_and_typed_bindings` | DAG_ACCEPTED |
| V2-FR-006, V2-FR-015, V2-SEC-010 | ADR-0010 | Runbook Registry | 6 | `test_runbook_version_digest_compile` | RUNBOOK_ACCEPTED |
| V2-FR-007 | ADR-0016 | Agentic Escalation | 8 | `test_hybrid_explicit_escalation_budget` | HYBRID_ACCEPTED |
| V2-FR-008, V2-NFR-014 | ADR-0015 | Result Shaper / Artifact Store | 4/5 | `test_result_shaping_and_artifact_ref` | BATCH_ACCEPTED / DAG_ACCEPTED |
| V2-FR-009, V2-FR-024 | ADR-0009 | Checkpoint/Lease Store | 5 | `test_resume_after_lease_recovery` | DAG_ACCEPTED |
| V2-FR-010..011 | ADR-0008/0009 | Result Aggregator | 4/5 | `test_partial_success_explicit` | BATCH_ACCEPTED / DAG_ACCEPTED |
| V2-FR-012 | ADR-0004 | Tool Registry | 1 | registry schema conformance | REGISTRY_ACCEPTED |
| V2-FR-013 | ADR-0005 | Capability Projection | 1 | `test_projection_denies_unlisted_capability` | REGISTRY_ACCEPTED |
| V2-FR-014, V2-SEC-002/006 | ADR-0006/0007 | Credential Broker | 1/2 | credential redaction/readiness/least privilege evidence | REGISTRY_ACCEPTED / DIRECT_READ_ACCEPTED |
| V2-FR-016, V2-SEC-012 | ADR-0009 | Transform Engine | 5 | `test_transform_no_eval_or_code` | DAG_ACCEPTED |
| V2-FR-017 | ADR-0011 | Policy Engine | 1 | `test_policy_dry_run_per_node` | REGISTRY_ACCEPTED |
| V2-FR-018, V2-SEC-004 | ADR-0013 | Idempotency Store | 3 | duplicate mutation/replay tests | DIRECT_MUTATION_ACCEPTED |
| V2-FR-020, V2-SEC-023 | ADR-0015/0018 | Evidence Layer | 4/9 | manifest/artifact integrity tests | V2_PRODUCTION_READY |
| V2-FR-021 | ADR-0004 | Capability Health | 1 | health state conformance | REGISTRY_ACCEPTED |
| V2-FR-022 | ADR-0015 | Replay Simulation | 5/9 | simulation does not call mutation backends | V2_PRODUCTION_READY |
| V2-FR-023, V2-NFR-019 | ADR-0008 | Scheduler | 4 | 429/circuit breaker/adaptive concurrency tests | BATCH_ACCEPTED |
| V2-FR-025, V2-NFR-009/020 | ADR-0017 | Compatibility Layer | all | v1 regression + shadow-read comparison | V2_PRODUCTION_READY |
| V2-SEC-001/003/009/020 | ADR-0011 | Policy Engine | 1+ | fail-closed/per-node scope suite | each affected gate |
| V2-SEC-005/011 | ADR-0012 | Approval Store | 3/5 | digest mismatch/replay/TOCTOU tests | DIRECT_MUTATION_ACCEPTED |
| V2-SEC-007/015..018 | ADR-0003/0019 | Execution Sandbox | 2/3/7 | path/service/egress/socket negative tests | V2_PRODUCTION_READY |
| V2-SEC-013/014/019 | ADR-0004/0005 | Registry/Projection/Schema | 1 | malicious metadata + secret field tests | REGISTRY_ACCEPTED |
| V2-SEC-021 | ADR-0014 | Saga/Compensation | 5/6 | unsafe compensation rejection | DAG_ACCEPTED |
| V2-SEC-022, V2-NFR-008/011 | ADR-0008 | Budgets/Scheduler | 4 | fan-out budget/backpressure tests | BATCH_ACCEPTED |
| V2-SEC-024, V2-NFR-003..005/015 | ADR-0018 | Observability | 0/9 | telemetry cardinality/secret scan | V2_PRODUCTION_READY |
