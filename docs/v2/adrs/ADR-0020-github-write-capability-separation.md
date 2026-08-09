# ADR-0020 — Dedicated Write Capabilities for GitHub Mutations

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

**Status:** Proposed

## Context
ADR-0007 established least-privilege credentials in general terms. Phase 3
introduces writes, where credential blast radius becomes the dominant risk and
where reusing the accepted `github.read` capability would silently upgrade the
read path's authority.

## Decision
Introduce distinct write capabilities — `github.write.branch`,
`github.write.pr`, `github.write.merge` — each with its own readiness state,
its own repository-scoped installation and its own minimal permission set. The
`Administration` permission is never granted to any V2 capability, making
repository deletion unrepresentable rather than merely denied.

## Consequences
More capabilities to provision, probe and rotate; each write tool must resolve
exactly one capability. A healthy read path can never be used to mutate.

## Alternatives
A single `github.write` capability (simpler, larger blast radius); reusing
`github.read` with expanded permissions (rejected: violates Phase 2 acceptance
assumptions).

## Security implications
Directly addresses T3-04, T3-09 and T3-10. Removes the class of bug where a
policy mistake yields administrative authority.

## Operational implications
Permission drift must be detected by probe, and a permission *superset* is a
failure condition, not an acceptable convenience.

## Open questions
Whether `github.write.merge` warrants a separate installation from
`github.write.pr`; OD-016 remains open on App vs fine-grained token.
