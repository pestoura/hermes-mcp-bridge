# Functional Requirements

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

| ID | Requirement |
|---|---|
| V2-FR-001 | DIRECT read-only operations must execute without the Hermes LLM. |
| V2-FR-002 | BATCH must support multiple independent operations in one bridge request. |
| V2-FR-003 | Independent BATCH nodes should execute concurrently within configured limits. |
| V2-FR-004 | DAG must support explicit node dependencies. |
| V2-FR-005 | DAG bindings must be typed and schema validated. |
| V2-FR-006 | Runbooks must be versioned and referencable by immutable version/digest. |
| V2-FR-007 | HYBRID agentic escalation must be explicit and reason-coded. |
| V2-FR-008 | Large results must support shaping and artifact references. |
| V2-FR-009 | Long DAG/runbook executions must support checkpoint/resume/recovery. |
| V2-FR-010 | Partial success must be represented explicitly. |
| V2-FR-011 | Every node must produce an independently addressable execution result/status. |
| V2-FR-012 | The Tool Registry must normalize tools from APIs, CLI wrappers, native tools, plugins and internal MCPs. |
| V2-FR-013 | Capability Projection must expose only authorized tool/schema subsets to clients. |
| V2-FR-014 | Credential resolution must use capability IDs rather than exposing secret locations or values. |
| V2-FR-015 | Runbooks must support deterministic validation/compilation before promotion. |
| V2-FR-016 | DAG/BATCH must support bounded deterministic transforms such as select/filter/map/count/extract without arbitrary code. |
| V2-FR-017 | BATCH/DAG must support policy simulation (`dry_run`) before execution. |
| V2-FR-018 | Mutating nodes must support idempotency keys where the backend semantics allow replay protection. |
| V2-FR-019 | Scheduler must support cancellation/deadline propagation. |
| V2-FR-020 | Execution must support signed/sanitized result manifests and artifact provenance. |
| V2-FR-021 | Capability health must distinguish AVAILABLE, DEGRADED, UNAVAILABLE and UNAUTHORIZED. |
| V2-FR-022 | Replay simulation must permit orchestration debugging with recorded outputs without replaying external mutations. |
| V2-FR-023 | Provider circuit breakers and adaptive concurrency must be supported. |
| V2-FR-024 | Long-running DAG execution must support lease/heartbeat semantics and dead-letter/manual-intervention state. |
| V2-FR-025 | V1 agentic tools must remain available during controlled v2 migration. |
