# ADR-0011 — Per-Node Policy and Governance

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
A single BATCH/DAG request may mix operations with different resources, risks and credentials.

## Decision
Evaluate principal, resource scope, policy action, risk/mutation class, quota/budget, approval, lock and credential capability independently for every node. Unknown/missing policy fails closed.

## Consequences
More evaluation work but prevents batch-level authority from leaking to unrelated operations.

## Alternatives
One approval/policy decision per whole request.

## Security implications
Core defense against confused deputy, cross-project confusion and scope bypass.

## Operational implications
Policy latency/caching/versioning must be observable and deterministic; dry-run simulation should expose per-node decisions.

## Open questions
Policy-as-code engine/format and principal/tenant model.
