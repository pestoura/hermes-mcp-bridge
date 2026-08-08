# V2 Open Decisions

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

These items are intentionally unresolved. They must not be inferred as implemented decisions.

| ID | Open decision | Main options / evaluation criteria |
|---|---|---|
| OD-001 | Versioning model | `/v2` namespace vs versioned tools vs capability negotiation vs schema/protocol version; ADR-0017 |
| OD-002 | Runbook DSL | YAML/JSON/domain model; typed bindings, reviewability, canonicalization, tooling |
| OD-003 | Durable queue/store | SQLite extension, dedicated queue/store, other; recovery/concurrency/operability |
| OD-004 | Artifact store | local content-addressed store, object storage, other; ACL, digest, retention, size |
| OD-005 | Credential provider backend | restricted file/keyring/Vault/cloud secret manager; bootstrap and rotation |
| OD-006 | Concurrency defaults | global/request/provider/resource/credential worker limits and adaptive policy |
| OD-007 | Principal/tenant model | caller identity assurance, tenant isolation, delegation and resource scopes |
| OD-008 | Approval UX | MCP interaction, external approval channel, expiry/nonce visibility, plan diff |
| OD-009 | Sandbox implementation | process/container/OS controls, Docker proxy, filesystem/network constraints |
| OD-010 | Result/artifact retention | default TTL, legal/operational evidence needs, deletion policy |
| OD-011 | Retry defaults | per security tier/backend and interaction with idempotency/deadline |
| OD-012 | Capability discovery protocol | static manifest, negotiated manifest, refresh/cache semantics |
| OD-013 | Dynamic vs static projection | context reduction vs predictability/auditability |
| OD-014 | Internal MCP proxying | normalized wrapper per tool vs controlled proxy layer; trust/metadata validation |
| OD-015 | Home Assistant projection model | safe read-only MVP, mutation tiers, entity/service resource scopes |
| OD-016 | GitHub credential model | GitHub App vs fine-grained tokens for MVP; installation/repository scopes |
| OD-017 | Policy-as-code format/engine | existing JSON evolution vs dedicated machine-readable policy engine; testability |
| OD-018 | Canonical plan/runbook serialization | deterministic JSON/CBOR/other; hashing compatibility/versioning |
| OD-019 | Runbook signing | when required, signer/key lifecycle, verification and promotion |
| OD-020 | Agentic escalation thresholds | enabled reason codes, confidence criteria, token/context budgets |
| OD-021 | Replay simulation format | recorded node outputs, schema/version drift handling, artifact references |
| OD-022 | Production SLO targets | direct latency, batch queueing, error rate, recovery, agentic escalation rate |
| OD-023 | Cost-aware planning | selection rules using latency/cost/rate-limit/LLM-required metadata |
| OD-024 | Transformation node DSL | exact safe operations and type system without general code execution |
| OD-025 | RITMO integration | only after fresh runtime/API discovery; no design assumption of current availability |
