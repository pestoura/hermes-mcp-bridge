# ADR-0026 — Runbook Least Privilege by Computed Capability Set

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

**Status:** Proposed

## Context
ADR-0007 requires least-privilege credentials and Phase 3 splits GitHub read
from write. A runbook aggregates several tools, so its authority is easy to
over-declare: an author adds a write capability "for a later version", or keeps
a capability after removing the node that needed it. Over-declaration silently
widens the blast radius of every future execution.

## Decision
The runbook's capability set is **computed** at admission from its pinned tool
references and must equal the declared set **exactly** — a superset and a subset
both fail. Administrative capabilities are excluded by capability, so no runbook
can reference them. At invocation the effective authority is the intersection of
runbook requirements, caller authorization, policy allowance and broker
readiness; if that intersection is not equal to the runbook's requirements the
execution is denied before any node runs. Credentials are projected per node for
the duration of that node, never merged into a runbook-wide super-credential,
and appear anywhere else only as capability IDs and status.

## Consequences
Removing a node forces a capability declaration update and therefore a version
bump. There is no partial execution of a mutating runbook because only some
capabilities were available. Authors lose the convenience of pre-declaring
future needs.

## Alternatives
Trust declared capabilities; allow supersets with a warning; resolve one merged
credential for the whole execution; check capabilities lazily at each node.

## Security implications
Enforces V2-SEC-025 and V2-SEC-013: metadata never expands authority, and the
worst-case operation is unreachable rather than conditionally blocked. Per-node
projection bounds the window in which any credential is usable.

## Operational implications
Requires a per-tool capability derivation that is itself trustworthy, and a
readiness probe that runs before credential resolution. Capability snapshot
drift between approval and execution must deny rather than proceed.

## Open questions
The credential provider backend (OD-005) and the GitHub credential model
(OD-016) remain open; principal/tenant modelling (OD-007) determines how caller
authorization is established.
