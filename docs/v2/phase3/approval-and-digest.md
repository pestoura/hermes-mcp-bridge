# Phase 3 Operation Digest and Approval Model

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

Specializes ADR-0012 for a single-node DIRECT mutation. Phase 3 has no DAG, so
the "plan digest" of ADR-0012 degenerates to an **operation digest** with the
same binding guarantees.

## Canonical operation digest

```text
operation_digest = SHA-256(canonical_json({
    "schema": "v2.phase3.operation.1",
    "operation": "<canonical tool id>",
    "capability": "<write capability id>",
    "repository": "owner/repo",
    "arguments": <canonicalized, fully-resolved typed arguments>,
    "preconditions": {
        "base_sha" | "expected_head_sha": "<40-hex>",
        "required_checks_policy": "<policy identifier>"
    },
    "policy_version": "<policy document version>",
    "registry_snapshot_hash": "<capability snapshot hash>"
}))
```

Rules:

- Canonical JSON uses the same deterministic serialization already accepted for
  the Phase 1 capability snapshot; the exact format decision is OD-018 and must
  be reused, not re-invented.
- The digest covers the **resolved** arguments. Nothing may be defaulted,
  templated or expanded after the digest is computed.
- `registry_snapshot_hash` and `policy_version` are inside the digest, so a
  registry or policy change invalidates outstanding approvals.
- Free-text editorial fields (PR title/body) are inside the digest as data: an
  edited body means a new approval.

## Approval record

| Field | Purpose |
|---|---|
| `approval_id` | Opaque identifier, not guessable |
| `principal` | Who requested |
| `approver` | Who approved (must differ from `principal` for `merge_pr`) |
| `operation_digest` | Exact binding |
| `scope` | Repository + operation; never wildcard |
| `nonce` | Single-use anti-replay value |
| `expires_at` | Short TTL; proposed 15 minutes for merges, 60 for create ops |
| `consumed_at` | Set atomically at consumption |
| `trust_context` | Channel/assurance level of the approval |

## Consumption rules

1. Consumption is **atomic** — a single conditional store update that both
   verifies `consumed_at IS NULL` and sets it. A losing racer gets
   `APPROVAL_ALREADY_CONSUMED`.
2. Approval is consumed **before** the provider call. If the call then fails
   cleanly, the approval is *not* silently restored; a new approval is required.
   This trades convenience for the elimination of replay.
3. Digest mismatch, expiry, wrong scope, wrong principal or unknown approval are
   all stable DENY reason codes with no differential timing or detail that would
   leak approval-store state.
4. Approvals are never inferred from a prior successful operation, from session
   state, or from the read path's authorization.

## Approval requirement by operation (proposed)

| Operation | Policy default |
|---|---|
| `github.create_branch` | `APPROVAL_REQUIRED` initially; may be relaxed to `ALLOW` for a narrow policy-declared ref prefix after evidence |
| `github.create_pr` | `APPROVAL_REQUIRED` |
| `github.merge_pr` | `APPROVAL_REQUIRED` with a distinct human approver, always |
| anything else | `DENY` |
