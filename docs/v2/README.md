# Hermes MCP Bridge v2 — Architecture Baseline

> **V2 · IMPLEMENTATION IN PROGRESS · PHASES 0–1 ACCEPTED · PHASE 2 CONNECTED ACCEPTANCE IN PROGRESS · V1 PRESERVED**
>
> Baseline date: **2026-08-08**. Repository: `pestoura/hermes-mcp-bridge`.

This directory records the evolution of the existing Hermes MCP Bridge into a **Hermes Execution Gateway / Secure Execution Control Plane**. V2 proceeds through explicit evidence gates. Phase 0 (`BASELINE_ACCEPTED`) and Phase 1 (`REGISTRY_ACCEPTED`) are accepted. Phase 2 (GitHub DIRECT Read-Only MVP) has its repository-side DIRECT executor, least-privilege GitHub App provider, canary, connected collector, mint/rotation helper and Jarvas launcher implemented; the real 15-sample Jarvas/Hermes acceptance is still required before `DIRECT_READ_ACCEPTED` may be declared. The operational V1 remains exactly 27 MCP tools.

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

The isolated `hermes_mcp_bridge.v2` package provides the accepted canonical registry foundation:

- typed `ToolDefinition` schema and invariants;
- security tiers and explicit mutation/idempotency classes;
- seven-state capability readiness model;
- deterministic versioned capability snapshot hashing;
- fail-closed static policy evaluation with stable reason codes;
- authorized-only deterministic capability projection;
- credential capability/broker contract without credential material;
- audit-safe canonical serialization;
- V1 isolation: no V1 module imports V2 and the V1 surface remains 27 tools.

## Active phase

### Phase 2 — GitHub DIRECT Read-Only MVP

Repository-side implementation includes:

- `github.get_repo`;
- `github.get_pr`;
- `github.get_checks`;
- `github.get_issue`;
- `github.search`;
- exact repository scope enforcement;
- file-backed GitHub App authorization with classic/broad PAT rejection;
- GitHub App installation-token mint/rotation with exact permission/repository validation;
- DIRECT canary with V1 fallback preserved outside acceptance;
- fail-closed connected collector for exactly 5 tools × 3 repetitions;
- real Hermes `state.db` session token accounting for the V1 shadow;
- full normalized semantic digest comparison;
- canonical Jarvas connected launcher;
- mechanically verified isolated read-only V1 shadow so the non-mutation basis is a runtime property rather than an operator string.

The original connected evidence validator remains mandatory. Automated promotion additionally requires the stricter combined gate:

```text
scripts/validate_v2_phase2_connected_gate.py
```

which binds the 15-sample evidence to a live isolated-shadow proof for the same source commit and repository scope.

`DIRECT_READ_ACCEPTED` is **not** declared until this strict connected gate returns zero failures on actual Jarvas/Hermes. ChatGPT's GitHub connector and repository CI are prerequisites/supporting evidence, not substitutes for that connected run.

Relevant Phase 2 documents:

- `phase2-connected-acceptance.md` — connected sample/evidence contract (INNER gate);
- `phase2-final-outer-gate.md` — OUTER final gate: internal-tool provenance plus
  out-of-band real-state integrity. REQUIRED for a formal `ACCEPTED`; status is
  still NOT ACCEPTED until a real out-of-band Jarvas run passes;
- `github-app-runtime-credential.md` — least-privilege GitHub App mint/rotation boundary;
- `phase2-jarvas-connected-launcher.md` — one-shot Jarvas execution path;
- `phase2-isolated-readonly-shadow.md` — mechanical proof for V1 shadow non-mutation.

## Next gated phase

Phase 3 — GitHub DIRECT mutations — remains blocked until Phase 2 produces:

```text
failures=[]
gate=DIRECT_READ_ACCEPTED
```

There is no early implementation/promotion of Phase 3 from CI-only or mock evidence.

A **preparation-only** design lane for Phase 3 exists in `phase3/`. It contains
the mutation threat model, credential split, operation semantics, governed merge
policy, idempotency/concurrency model, approval/digest model, audit contract,
rollback rules, fail-closed acceptance criteria and the planned mutation test
matrix. It defines no tool, changes no gate and is not acceptance evidence.

## Document map

- `architecture/` — as-is, target architecture, execution modes, registries, credentials, governance, scheduling, result shaping and observability.
- `security/` — threat model, hardening prerequisites and trust boundaries.
- `requirements/` — functional, security and non-functional requirements plus traceability.
- `contracts/` — conceptual DIRECT, BATCH, DAG, RUNBOOK and HYBRID contracts.
- `adrs/` — architectural decision records and remaining open questions.
- `phase3/` — Phase 3 preparation-only design lane (no implementation).
- `evidence/` — retained acceptance evidence and release manifests.
- `roadmap.md` — phased implementation plan and gates.
- `risk-register.md` — V2 risks and mitigations.
- `open-decisions.md` — decisions intentionally left open.

## Existing V1 foundations to reuse

V1 already provides deterministic policy evaluation, fail-closed handling, persistent/single-use approvals, HMAC-signed result manifests in production, plans, checkpoints/continuations, sagas/compensation records, resource locks and quotas. V2 evolves these controls instead of replacing them without cause.

## Current non-goals / deferred capabilities

- no change to the 27-tool V1 contract during Phase 2 acceptance;
- no production promotion of Phase 2 before connected Jarvas evidence;
- no GitHub DIRECT mutation implementation before Phase 2 acceptance;
- no claim that later integrations are currently healthy or authorized;
- no substitution of CI/mock/ChatGPT connector evidence for the required Jarvas gate.
