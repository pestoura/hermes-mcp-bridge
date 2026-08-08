# V2 Risk Register

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

Ratings are planning-level and must be recalibrated during Phase 0/implementation.

| ID | Risk | Initial severity | Planned treatment / gate |
|---|---|---|---|
| V2-R-001 | Direct execution with overprivileged credentials | Critical | GitHub App/fine-grained capability credentials; block mutations before DIRECT_MUTATION_ACCEPTED |
| V2-R-002 | Generic shell/host surface projected to client | Critical | typed tools only; sandbox allowlists; ADR-0003/0019 |
| V2-R-003 | Confused deputy / resource-scope bypass | Critical | per-node principal/scope/policy; immutable digests; negative tests |
| V2-R-004 | Approval replay or TOCTOU | Critical | canonical plan digest, expiry/nonce, atomic consumption |
| V2-R-005 | Duplicate mutation from retries/resume | High | idempotency keys/store, retry classes, optimistic concurrency |
| V2-R-006 | DAG fan-out / amplification DoS | High | max nodes/calls/parallelism/runtime, bounded queues/backpressure |
| V2-R-007 | Provider 429/outage causes retry storm | High | circuit breakers, adaptive concurrency, Retry-After/backoff/jitter |
| V2-R-008 | Malicious internal MCP/plugin metadata expands authority | High | normalized registry, projection allowlist, metadata not trusted by default |
| V2-R-009 | Prompt/tool injection influences deterministic mutation | High | external content treated as data; agent output cannot bypass policy/approval |
| V2-R-010 | Secret exfiltration through result/artifact/telemetry | Critical | secret-aware schemas, fail-closed redaction, artifact ACL/integrity, telemetry rules |
| V2-R-011 | Existing secret hygiene (`SUDO_PASSWORD`, env backups) expands blast radius | Critical | hardening prerequisite before privileged direct execution |
| V2-R-012 | Credential marked configured but expired/unhealthy | High | credential readiness state; fail missing/unhealthy capability |
| V2-R-013 | Unsafe compensation corrupts cross-system state | High | explicit compensatable metadata; governed compensation; manual intervention default |
| V2-R-014 | Runbook supply-chain or malicious change | High | version control, review/tests, canonical digest, optional signing/promotion controls |
| V2-R-015 | Artifact tampering / stale evidence | High | digest/signature/provenance/retention policy |
| V2-R-016 | Schema/binding confusion executes wrong arguments | High | typed schemas/bindings; fail ambiguity; no eval/shell interpolation |
| V2-R-017 | Docker raw socket exposes host root-equivalent authority | Critical | mediated Docker surface/socket proxy; strict operation allowlist |
| V2-R-018 | SSRF/uncontrolled network egress | High | destination/egress policy in wrappers/sandbox |
| V2-R-019 | V2 breaks v1 or changes semantics silently | High | versioning ADR, feature flags, regression tests, canary, rollback |
| V2-R-020 | Shadow mode accidentally duplicates mutations | Critical | shadow reads only; mutation shadow execution prohibited |
| V2-R-021 | Incorrect token-savings claim based on broad audit | Medium | representative Phase-0 benchmark; treat 516,082-token audit only as evidence of possible context growth |
| V2-R-022 | Excessive observability cardinality/cost | Medium | bounded labels, histograms/SLO design, no IDs/secrets as unbounded labels |
| V2-R-023 | Durable queue/store loss leaves unknown committed state | High | checkpoints/evidence/idempotency, leases, manual-intervention state, recovery tests |
| V2-R-024 | Capability projection/cache becomes stale | Medium | manifest hash/version/health and explicit refresh/negotiation semantics |
| V2-R-025 | RITMO assumed available based on documentation only | Medium | keep status NOT CONFIRMED until runtime/API evidence and an integration-specific gate |
