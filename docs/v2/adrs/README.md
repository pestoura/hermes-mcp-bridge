# V2 Architectural Decision Records

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

All ADRs are **Proposed** unless explicitly promoted later through the repository
governance process. ADR-0024..ADR-0027 are **Accepted** for Phase 5: they are
implemented behind `DAG_FEATURE_ENABLED` and bound by the executable
`DAG_ACCEPTED` gate.

| ADR | Decision topic |
|---|---|
| ADR-0001 | V2 evolves current `hermes-mcp-bridge` |
| ADR-0002 | Deterministic-first execution |
| ADR-0003 | Typed tools instead of generic shell exposure |
| ADR-0004 | Canonical Tool Registry |
| ADR-0005 | Capability Projection |
| ADR-0006 | Credential Broker abstraction |
| ADR-0007 | Least-privilege credentials |
| ADR-0008 | BATCH execution semantics |
| ADR-0009 | DAG execution semantics |
| ADR-0010 | Skill vs Runbook |
| ADR-0011 | Per-node policy/governance |
| ADR-0012 | Approval bound to immutable plan digest |
| ADR-0013 | Idempotency and replay protection |
| ADR-0014 | Saga/compensation semantics |
| ADR-0015 | Result shaping and artifact references |
| ADR-0016 | Agentic fallback / HYBRID escalation |
| ADR-0017 | Versioning and backward compatibility |
| ADR-0018 | Observability and token economics |
| ADR-0019 | Execution sandbox boundaries |
| ADR-0020 | GitHub write capability separation (Phase 3 preparation) |
| ADR-0021 | Operation digest for single-node mutations (Phase 3 preparation) |
| ADR-0022 | GitHub mutation idempotency and optimistic concurrency (Phase 3 preparation) |
| ADR-0023 | Governed merge and destructive-operation exclusion (Phase 3 preparation) |
| ADR-0024 | Durable DAG state store — SQLite/WAL with fence tokens (Phase 5, closes OD-003) |
| ADR-0025 | Canonical plan digest — deterministic JSON, versioned (Phase 5, closes OD-018 for plans) |
| ADR-0026 | TRANSFORM nodes are a closed operation set (Phase 5, closes OD-024) |
| ADR-0027 | DAG replay format — shaped results, providers disabled (Phase 5, closes OD-021) |
