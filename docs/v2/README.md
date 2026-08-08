# Hermes MCP Bridge v2 — Architecture Baseline

> **V2 · IMPLEMENTATION IN PROGRESS · PHASE 0 ACCEPTED · V1 TOOL CONTRACT AND SEMANTICS PRESERVED**
>
> Baseline date: **2026-08-08**. Repository: `pestoura/hermes-mcp-bridge`.

This directory records the evolution of the existing Hermes MCP Bridge into a **Hermes Execution Gateway / Secure Execution Control Plane**. V2 implementation is in progress under gated phases: Phase 0 (AS-IS + connected baseline) is **ACCEPTED** as of 2026-08-08 with real runtime evidence, and Phase 1 (Tool Registry) is next. The operational v1 remains the current execution path; its tool contract and semantics are preserved and unchanged by this work.

See `evidence/README.md` for the Phase 0 acceptance evidence index and `roadmap.md` for phase gates.

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

The v1 control plane can require a deterministic request to traverse ChatGPT -> Bridge -> Hermes Agent -> LLM -> skill -> terminal/tool -> target. V2 plans a typed execution surface so known, authenticated operations can execute without an intermediate Hermes LLM, reducing tokens, latency, failure points and context growth while preserving policy, approvals, audit, quotas, locks, manifests and evidence.

The audited broad runtime investigation consumed approximately **516,082 total tokens** (506,390 input / 9,692 output). This is evidence that broad agentic investigations can become context-heavy; it is **not** a normal-call estimate or a v2 performance promise.

## Document map

- `architecture/` — as-is, target architecture, execution modes, registries, credentials, governance, scheduling, result shaping and observability.
- `security/` — threat model, hardening prerequisites and trust boundaries.
- `requirements/` — functional, security and non-functional requirements plus traceability.
- `contracts/` — conceptual DIRECT, BATCH, DAG, RUNBOOK and HYBRID contracts.
- `adrs/` — proposed architectural decision records.
- `roadmap.md` — phased implementation plan and gates.
- `risk-register.md` — v2 risks and mitigations.
- `open-decisions.md` — decisions intentionally left open.

## Existing v1 foundations to reuse

V1 already provides important foundations documented elsewhere in this repository: deterministic policy evaluation, fail-closed handling, persistent/single-use approvals, HMAC-signed result manifests in production, plans, checkpoints/continuations, sagas/compensation records, resource locks and quotas. V2 should evolve these controls instead of replacing them without cause.

## Non-goals of this baseline

- no v2 runtime implementation;
- no v1 semantic change;
- no deployment/container/systemd change;
- no production policy change;
- no credential or secret change;
- no claim that planned integrations are currently healthy or authorized.
