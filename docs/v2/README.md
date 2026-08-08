# Hermes MCP Bridge v2 — Architecture Baseline

> **V2 · IMPLEMENTATION IN PROGRESS · PHASES 0–1 ACCEPTED · V1 TOOL CONTRACT AND SEMANTICS PRESERVED**
>
> Baseline date: **2026-08-08**. Repository: `pestoura/hermes-mcp-bridge`.

This directory records the evolution of the existing Hermes MCP Bridge into a **Hermes Execution Gateway / Secure Execution Control Plane**. V2 implementation is proceeding through explicit evidence gates: Phase 0 (AS-IS + connected baseline) is **ACCEPTED** and Phase 1 (canonical Tool Registry) is **ACCEPTED** as of 2026-08-08. Phase 2 (GitHub DIRECT Read-Only MVP) is next. The operational V1 remains the current execution path; its 27-tool contract and semantics are preserved and unchanged by Phases 0–1.

See `evidence/README.md` for the acceptance evidence index and `roadmap.md` for phase gates.

## Core principle

```text
DETERMINISTIC WORK -> CODE
KNOWN WORKFLOW    -> RUNBOOK
REASONING         -> LLM
```

Preferred execution path:

```text
DIRECT > BATCH > DAG/RUNBOOK > AGENTIC
```

`HYBRID` is an explicit, controlled combination in which deterministic execution runs first and agentic escalation is used only when a declared condition requires reasoning.

## Why v2

The V1 control plane can require a deterministic request to traverse ChatGPT -> Bridge -> Hermes Agent -> LLM -> skill -> terminal/tool -> target. V2 introduces a typed execution surface so known, authenticated operations can progressively execute without an intermediate Hermes LLM, reducing tokens, latency, failure points and context growth while preserving policy, approvals, audit, quotas, locks, manifests and evidence.

The accepted Phase 0 connected baseline measured **519,048 real LLM tokens** across 9/9 uncontaminated representative V1 samples (read, sandbox mutation and bounded agentic scenarios). That figure is a baseline for later DIRECT/BATCH/HYBRID comparison, not a per-call estimate or a V2 performance promise.

## Accepted phases

### Phase 0 — `BASELINE_ACCEPTED`

Real Jarvas/Hermes connected evidence established the V1 token/latency/API-call baseline, runtime identity, security posture and trust boundaries. See `evidence/README.md`.

### Phase 1 — `REGISTRY_ACCEPTED`

The isolated `hermes_mcp_bridge.v2` package now provides the accepted canonical registry foundation:

- typed `ToolDefinition` schema and invariants;
- security tiers and explicit mutation/idempotency classes;
- seven-state capability readiness model;
- deterministic versioned capability snapshot hashing;
- fail-closed static policy evaluation with stable reason codes;
- authorized-only deterministic capability projection;
- credential capability/broker contract without credential material;
- audit-safe canonical serialization that excludes free-text editorial metadata;
- V1 isolation: no V1 module imports V2 and the V1 surface remains 27 tools.

Acceptance is bound to integrated `main` commit
`4bc999084b88cc5ef5346f21c9f2e09717c63568` and its durable CI evidence. It does
**not** imply that a real credential backend, principal/tenant authorization,
dynamic capability discovery/projection, registry persistence/signing or later
execution engines are implemented.

## Next phase

Phase 2 is the **GitHub DIRECT Read-Only MVP**. Before real activation it must
perform fresh Jarvas-side discovery of the GitHub credential/provider path,
introduce dedicated least-privilege read credentials, implement the five typed
read operations, and compare DIRECT results with the V1 agentic path in shadow
mode. No availability or authorization is inferred from the ChatGPT GitHub
connector.

## Document map

- `architecture/` — as-is, target architecture, execution modes, registries, credentials, governance, scheduling, result shaping and observability.
- `security/` — threat model, hardening prerequisites and trust boundaries.
- `requirements/` — functional, security and non-functional requirements plus traceability.
- `contracts/` — conceptual DIRECT, BATCH, DAG, RUNBOOK and HYBRID contracts.
- `adrs/` — architectural decision records and remaining open questions.
- `evidence/` — retained acceptance evidence and release manifests.
- `roadmap.md` — phased implementation plan and gates.
- `risk-register.md` — V2 risks and mitigations.
- `open-decisions.md` — decisions intentionally left open.

## Existing V1 foundations to reuse

V1 already provides important foundations documented elsewhere in this repository: deterministic policy evaluation, fail-closed handling, persistent/single-use approvals, HMAC-signed result manifests in production, plans, checkpoints/continuations, sagas/compensation records, resource locks and quotas. V2 evolves these controls instead of replacing them without cause.

## Current non-goals / deferred capabilities

- no V1 semantic or 27-tool contract change from Phases 0–1;
- no production V2 DIRECT tool exposure yet;
- no real generalized credential backend yet;
- no principal/tenant authorization model yet;
- no dynamic capability discovery/projection yet;
- no registry persistence/signing decision yet;
- no claim that future integrations are currently healthy or authorized.
