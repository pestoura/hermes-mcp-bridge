# ADR-0001 — V2 Evolves the Current Bridge

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
The current repository already owns MCP transport, governance, approvals, manifests, plans, checkpoints, sagas, locks and quotas. Creating a second product would duplicate governance and migration complexity.

## Decision
Evolve `pestoura/hermes-mcp-bridge` in place. Use “Hermes Execution Gateway” / “Secure Execution Control Plane” as architectural descriptions, not a new repository/product.

## Consequences
Shared history and controls; migration must preserve v1 semantics and rollback.

## Alternatives
New repository/service; fork; replacement rewrite.

## Security implications
Avoids duplicating security controls but increases need for strict feature isolation/versioning.

## Operational implications
V1 and v2 surfaces may coexist during migration.

## Open questions
Final protocol/versioning mechanism and feature-flag layout.
