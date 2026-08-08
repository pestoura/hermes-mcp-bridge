# ADR-0017 — Versioning and Backward Compatibility

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
V1 is operational while v2 introduces new semantics. Silent semantic changes would break clients and complicate rollback.

## Decision
V1 remains available throughout migration; use explicit versioning/feature flags, canary, shadow mode for reads and rollback to agentic v1 path. No real mutation is duplicated in shadow mode.

## Consequences
Temporary coexistence/complexity is accepted to reduce migration risk.

## Alternatives
Big-bang v2 replacement; silently change existing tools.

## Security implications
Version confusion must not allow weaker policies or replay across protocol versions.

## Operational implications
Shadow compare correctness/latency/tokens/API calls/failures for reads.

## Open questions
Choose among `/v2` namespace, versioned tools, capability negotiation and protocol/schema versioning (or a documented combination).
