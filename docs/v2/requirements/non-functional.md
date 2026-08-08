# Non-Functional Requirements

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

| ID | Requirement |
|---|---|
| V2-NFR-001 | DIRECT operations must not incur Hermes LLM tokens for execution. |
| V2-NFR-002 | BATCH/DAG parallelism must be bounded. |
| V2-NFR-003 | Metrics must distinguish all six execution modes. |
| V2-NFR-004 | Tracing must use bounded-cardinality attributes. |
| V2-NFR-005 | All executions and nodes must have stable request/execution identifiers. |
| V2-NFR-006 | Timeout/deadline must propagate through scheduler and backends. |
| V2-NFR-007 | Provider rate limits must use controlled backpressure. |
| V2-NFR-008 | Payload/result/request budgets must be enforceable before and during execution. |
| V2-NFR-009 | V1 rollback/agentic path must remain available during migration. |
| V2-NFR-010 | Direct-vs-agentic latency/token/API-call benchmarks must be measurable. |
| V2-NFR-011 | Scheduler must provide fair bounded work queues rather than unbounded fan-out. |
| V2-NFR-012 | Retries must honor global deadlines, provider hints, bounded backoff and jitter. |
| V2-NFR-013 | Capability snapshots must make executions reproducible at the control-plane metadata level. |
| V2-NFR-014 | Result shaping must expose raw-vs-returned byte measurements. |
| V2-NFR-015 | Policy, approval, credential-resolution, queue and backend latency must be observable. |
| V2-NFR-016 | Execution manifests must be sufficient for audit without requiring secret retention. |
| V2-NFR-017 | Registry/runbook schemas must be versionable without silent semantic changes. |
| V2-NFR-018 | Production mutation execution must remain recoverable through evidence/checkpoint/manual-intervention states. |
| V2-NFR-019 | Provider failures must be isolated using circuit-breaker semantics where appropriate. |
| V2-NFR-020 | The v2 implementation must support canary/shadow evaluation for reads without duplicating real mutations. |
