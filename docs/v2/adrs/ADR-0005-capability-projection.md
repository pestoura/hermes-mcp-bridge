# ADR-0005 — Capability Projection

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

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

## Open questions
Static vs dynamic projection and internal MCP proxying model.
