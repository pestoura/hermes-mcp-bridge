# ADR-0004 — Canonical Tool Registry

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

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

## Open questions
Registry storage/format, signing and schema migration process.
