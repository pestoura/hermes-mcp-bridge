# ADR-0005 — Capability Projection

> **V2 · PHASE 1 ACCEPTED · NO IMPACT ON V1**

**Status:** Accepted for the Phase 1 static projection model. `REGISTRY_ACCEPTED` evidence validated on integrated `main` commit `4bc999084b88cc5ef5346f21c9f2e09717c63568`.

## Context
Hermes can contain hundreds of tools/skills; exposing all capabilities increases context, confusion and attack surface.

## Decision
Project only policy-approved tools and minimum schemas from the internal registry to clients. Phase 1 deliberately keeps principal/scope context opaque and does not yet use it for authorization.

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
filtering. The accepted model is covered by the fail-closed Phase 1 evidence
indexed in `docs/v2/evidence/README.md`.

## Open questions
Static vs dynamic projection (OD-013) and the internal MCP proxying model
(OD-014) remain open. Phase 1 accepted **static** projection for this phase
only; dynamic projection is deferred, not rejected. Principal/tenant
authorization also remains deferred under OD-007 and is not implied by
`REGISTRY_ACCEPTED`.
