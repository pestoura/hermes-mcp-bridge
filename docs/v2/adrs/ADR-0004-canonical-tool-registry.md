# ADR-0004 — Canonical Tool Registry

> **V2 · PHASE 1 ACCEPTED · NO IMPACT ON V1**

**Status:** Accepted. Phase 1 implementation and `REGISTRY_ACCEPTED` evidence validated on integrated `main` commit `4bc999084b88cc5ef5346f21c9f2e09717c63568`.

## Context
Capabilities may come from native tools, CLI wrappers, APIs, plugins, internal MCPs or future connectors, each with different metadata.

## Decision
Create one canonical typed registry including schemas, risk/mutation class, policy action, scope, credential capability, retry/idempotency, concurrency/lock hints, backend/provenance, shaping, cost/stability/version and health.

## Consequences
Central source of truth and validation burden.

## Alternatives
Backend-specific catalogs; dynamic pass-through of MCP metadata.

## Security implications
Registry integrity becomes critical; untrusted metadata must not mutate authority.

## Operational implications
Supports capability snapshots and health states AVAILABLE/DEGRADED/UNAVAILABLE/UNAUTHORIZED.

## Phase 1 outcome
Implemented in `hermes_mcp_bridge.v2` as an isolated typed in-process model with
canonical JSON serialization and a SHA-256 `capability_snapshot_hash`. Health
states are `CONFIGURED`/`AVAILABLE`/`HEALTHY`/`READY`/`DEGRADED`/`UNAVAILABLE`/
`DENIED`, where `DENIED` is the state this ADR called `UNAUTHORIZED`.

The accepted implementation is covered by the Phase 1 fail-closed acceptance
harness and durable release evidence indexed in `docs/v2/evidence/README.md`.

## Open questions
Registry storage/format, signing and schema migration process — all still open
questions **of this ADR**; they are not tracked by an OD entry (OD-003 is the
durable queue/store decision, which is a different concern). Phase 1 chose
in-process typed objects plus canonical JSON serialization, and deliberately
did not choose a persistence backend. `REGISTRY_ACCEPTED` does not close these
questions.
