# Governance and Multi-Agent Control — 0.5.0

Bridge contract version: **0.5.0**.

## Scope and boundaries

The bridge validates, persists, and communicates governance policy. It does not pretend to execute multi-agent capabilities that Hermes upstream has not confirmed.

## Orchestration contract

Public modes: `auto`, `single`, `parallel`, `pipeline`, `review`.

Upstream effective modes: `auto`, `explicit` only.

Inputs using `auto|explicit` remain valid. `explicit` is treated as an explicit policy without breaking existing callers.

Agent card fields:
- `orchestration_contract_modes`: all requested modes.
- `upstream_effective_modes`: modes confirmed upstream.

Capability manifest `upstream_support`:
- `requested`: bridge-supported contract modes.
- `effective`: modes Hermes has confirmed.
- `unsupported`: requested modes not confirmed upstream.

## Policy engine

Decisions: `ALLOW`, `DENY`, `REQUIRE_APPROVAL`.

Rules are declarative and deterministic. No eval/exec/code execution. No dangerous regex or template execution.

Default posture: allow low-risk reads; mutations default to `REQUIRE_APPROVAL`.

High-risk trust labels with mutation default to `REQUIRE_APPROVAL`.

## Tool trust metadata

`ExtendedToolManifest` includes:
- `trust_level`
- `mutation_class`
- `reversible`
- `idempotency_class`
- `approval_requirement`
- `attestation_status`

Tool manifests are honest about supported capabilities.

## Approvals

Approvals are persistent, single-use by default, and transactional.

Statuses: `requested`, `approved`, `rejected`, `expired`, `consumed`, `stale`.

Identity assurance is `caller_asserted` until upstream provides verifiable identity.

## Provenance

Claims use `OBSERVED`, `DERIVED`, `INFERRED`, `UNVERIFIED`.

Result manifests include sanitized metadata only: never prompt text, raw outputs, or tool arguments.

HMAC-SHA256 signing is optional. Without configured secret: `signature_status=unsigned`.

## Migration from 0.4

`run_mappings` table is preserved. `approvals` table is added with idempotent creation.

Bridge version bump: `0.5.0`. Backward compatibility for the original 9 tools is preserved; 5 governance tools were added.
