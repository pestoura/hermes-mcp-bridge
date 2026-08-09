# Runbook Identity, Versioning and Immutable Pinning

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

## Identity

A runbook is identified by a triple that must be stable for its entire life:

| Field | Rule |
|---|---|
| `runbook_id` | `RB-<DOMAIN>-<SUBJECT>-<NNN>`, uppercase, ASCII, `^RB-[A-Z0-9]+(-[A-Z0-9]+)+-[0-9]{3}$`, max 64 chars. Example: `RB-GITHUB-PR-LIFECYCLE-001` |
| `version` | Semantic version `MAJOR.MINOR.PATCH`, no pre-release or build metadata in an admitted runbook |
| `runbook_digest` | SHA-256 over the canonical IR (see below), lowercase hex, 64 chars |

`runbook_id` is **not** reusable. Once admitted, the pair
`(runbook_id, version)` is immutable: any change of any admission-relevant byte
requires a new `version`. Re-admitting the same `(runbook_id, version)` with a
different `runbook_digest` is rejected with `RB_DIGEST_CONFLICT`, never
silently overwritten.

`runbook_id` MUST NOT collide with any tool name in the canonical tool registry
(ADR-0004). Runbooks and tools live in disjoint namespaces; a caller cannot
address a runbook as if it were a tool.

## Canonical IR and digest

Admission compiles the source definition once into a **canonical intermediate
representation (IR)**. The IR is deterministic, sorted, encoding-normalized and
excludes all editorial free text (descriptions, comments, changelog prose) so
that documentation edits cannot change execution identity — and, conversely, so
that no semantic change can hide inside a description.

`runbook_digest = SHA-256(canonical_bytes(IR))`, where `canonical_bytes` is the
serialization chosen by OD-018 and shared with `plan_digest` (see
`plan-digest-binding.md`). The IR carries `ir_schema_version`; a digest is only
comparable within the same `ir_schema_version`.

Fields included in the IR (digest-relevant):

- `runbook_id`, `version`, `ir_schema_version`
- ordered node list: node key, tool reference `(tool_name, tool_version)`,
  typed bindings, per-node policy class, per-node timeout, retry class,
  idempotency declaration, compensation declaration
- edge list (dependencies), canonically sorted
- parameter schema and output schema (canonicalized)
- `requires_capabilities`, credential capability IDs, resource scope
  expressions
- `policy_class`, `approval_class`, `destructive_action`, `rollback_support`
- `timeout_ms`, budget defaults
- `owner` identity reference (the accountable identity is execution-relevant)

Fields excluded from the IR (non-digest-relevant): human titles, descriptions,
rationale, links, review notes, `created_at`, and any registry bookkeeping
timestamps.

## Semantic version rules

Governed by ADR-0017 and refined here:

| Change | Required bump |
|---|---|
| Parameter added as **required**; parameter removed; type narrowed; output field removed | MAJOR |
| Node added or removed; edge changed; tool reference changed to a different tool | MAJOR |
| `destructive_action` false→true; `approval_class` weakened; `policy_class` weakened; capability set widened | MAJOR |
| Optional parameter added with a default; output field added; node retry/timeout relaxed within policy bounds | MINOR |
| Tool reference pinned to a newer compatible tool version; budget default tightened; compensation added | MINOR |
| Any change that alters no IR byte | not admissible as a new version (nothing to admit) |
| Editorial-only change | no new version; documentation may be updated in place |

A weakening change (looser policy, fewer approvals, broader capabilities) is
**always** MAJOR regardless of how small it looks. Tightening may be MINOR.

## Immutable version pinning

- Every runbook node pins its tool by `(tool_name, tool_version)` — never
  "latest". An unpinned or floating reference is rejected at admission with
  `RB_UNPINNED_REFERENCE`.
- Every invocation must address a runbook by `(runbook_id, version)` **and**
  carry the expected `runbook_digest`. A caller supplying only
  `(runbook_id, version)` is rejected with `RB_DIGEST_REQUIRED`; the registry
  never resolves an unstated digest on the caller's behalf.
- A runbook that composes another runbook pins it by
  `(runbook_id, version, runbook_digest)`. Composition depth is bounded
  (recommended max 2) and cycles are rejected with `RB_COMPOSITION_CYCLE`.
- Resolution is fail-closed: unknown id → `RB_UNKNOWN`; known id with unknown
  version → `RB_UNKNOWN_VERSION`; digest mismatch → `RB_DIGEST_MISMATCH`. No
  fallback to a neighbouring version under any condition.

## Lifecycle states

| State | Meaning | Invocable |
|---|---|---|
| `DRAFT` | Authored, not admitted | No |
| `ADMITTED` | Passed admission validation; not yet promoted | Only in an explicitly non-production promotion context |
| `ACTIVE` | Promoted for use | Yes |
| `DEPRECATED` | Superseded; still invocable, emits a deprecation signal | Yes |
| `YANKED` | Withdrawn for correctness/security reasons | **No** — `RB_YANKED`, fail-closed, no grace period |
| `ARCHIVED` | Retained for audit only | No |

State transitions are append-only registry events with actor, timestamp and
reason code. A yank takes effect for **new executions immediately**; executions
already in flight follow the cancellation rules in
`rollback-timeouts-and-budgets.md` and are never silently abandoned.

## Registry snapshot

The registry publishes a deterministic `runbook_snapshot_hash` over the sorted
set of `(runbook_id, version, runbook_digest, state)` tuples, mirroring the
Phase 1 `capability_snapshot_hash` pattern. Evidence records the snapshot hash
in force at execution time so that a control-plane state is reproducible
(V2-NFR-013).
