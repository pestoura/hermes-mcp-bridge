# ADR-0034 — Capability discovery is declare, probe, demote-only

- Status: Accepted (Phase 7)
- Supersedes: the `ADR-0025` proposal in the downstream design lane.
- Related: ADR-0004 (capability health), Phase 1 seven-state model.

## Context

If a runtime probe can *create* or *promote* a capability, the exposed surface
becomes a function of network weather rather than of reviewed code.

## Decision

Discovery has three stages. **Declare:** the manifest lists capability ids with
class, tool binding, credential capability, scopes, egress hosts and budgets;
the gateway rejects a manifest declaring a capability with no registered typed
tool, an unknown credential capability, a scope wider than the domain grants, or
a duplicate id. **Probe:** `health()` performs a bounded read-only call; a
`DIRECT_WRITE` capability is probed by a read that proves scope, never by a
trial mutation. **Demote only:** `apply_health` may move a capability only
downward in the ordering `DENIED < UNAVAILABLE < DEGRADED < CONFIGURED <
AVAILABLE < HEALTHY < READY`. The single exception is the initial classification
out of the declaration-time `CONFIGURED` placeholder, and there an inconclusive
**write** probe resolves to `UNAVAILABLE`, not `DEGRADED`.

Read capabilities may serve while `DEGRADED`, with an explicit marker on the
result. Write capabilities require `READY`.

## Consequences

- The exposed surface is deterministic: two independent builds on the same
  commit produce identical `capability_snapshot_hash` and
  `write_capability_digest`.
- A write-surface change is individually detectable even when the read surface
  is unchanged, because the write digest is computed separately.
