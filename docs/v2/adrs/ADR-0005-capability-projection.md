# ADR-0005 — Capability Projection

> **V2 · PHASE 1 CORE IMPLEMENTED · NOT YET ACCEPTED · NO IMPACT ON V1**

**Status:** Accepted in principle; Phase 1 core implemented, `REGISTRY_ACCEPTED` not declared.

## Context
Hermes can contain hundreds of tools/skills; exposing all capabilities increases context, confusion and attack surface.

## Decision
Project only principal/scope/policy-approved tools and minimum schemas from the internal registry to clients.

## Consequences
Smaller safer client surface; projection state must be observable/reproducible.

## Alternatives
Expose full catalog; rely solely on post-call policy.

## Security implications
Prevents unnecessary dangerous capability disclosure and reduces tool-injection surface.

## Operational implications
Capability discovery/refresh and client caching need defined semantics.

## Phase 1 outcome
`project_capabilities()` is deterministic and ordered by `tool_id`, projects
only `ALLOW` and explicitly flagged `APPROVAL_REQUIRED` tools, and emits a
strict non-secret field allow-list. Principal context is opaque and unused for
filtering.

## Open questions
Static vs dynamic projection (OD-013) and the internal MCP proxying model
(OD-014) remain open. Phase 1 selected **static** projection for this phase
only; dynamic projection is deferred, not rejected.
