# Phase 3 Idempotency, Replay Protection and Concurrency

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

Specializes ADR-0013 for GitHub mutations.

## Idempotency key

Server-side derived, deterministic, and scoped so that two different principals
or repositories can never share a record:

```text
idempotency_key = SHA-256(
    canonical_json({
        "principal": <principal_id>,
        "capability": <write capability id>,
        "repository": "owner/repo",
        "operation": "github.create_branch" | ...,
        "operation_digest": <see approval-and-digest.md>,
        "client_key": <optional caller-supplied opaque string>
    })
)
```

- A caller-supplied `client_key` narrows a record; it can never widen or
  substitute the derived scope.
- Records are stored server-side only and are never returned verbatim; the
  result exposes an `idempotency_status` of `NEW`, `REPLAYED` or `IN_PROGRESS`.
- Retention window: proposed 7 days for terminal records, and a shorter lease
  for `IN_PROGRESS` (see open items). Expiry is a config decision, not a code
  default that silently changes semantics.

## Record lifecycle

| State | Meaning | Behavior on a repeated request |
|---|---|---|
| `IN_PROGRESS` (leased) | A write was started and not yet resolved | Return `IN_PROGRESS`, do **not** issue a second write |
| `COMMITTED` | The provider confirmed the mutation | Return the stored shaped result, `REPLAYED` |
| `FAILED_CLEAN` | Provider rejected before any state change | A new attempt is allowed |
| `AMBIGUOUS` | Timeout/transport failure with unknown provider state | Reconciliation read required before any new attempt; never blind retry |

The intent record is written **before** the provider call (write-ahead), so a
crash between the call and the response still leaves an `AMBIGUOUS` record
rather than a silent gap.

## Optimistic concurrency

Every mutation carries an expected-state precondition and, where the provider
supports it, pushes the check server-side:

| Operation | Client precondition | Server-side enforcement |
|---|---|---|
| `create_branch` | `base_sha` pinned | Ref creation fails `422` if the ref exists |
| `create_pr` | `expected_head_sha` | Re-read of head before the call |
| `merge_pr` | `expected_head_sha` | `sha` parameter → provider returns `409` on drift |

Where the provider offers no server-side check, the re-read happens as late as
possible and the resulting window is documented and tested, not assumed absent.

## Locks

A short-lived server-side lock is taken on the tuple
`(repository, operation-family, target-ref-or-pr)` for the duration of a write.
The lock is advisory for coordination inside the bridge — it is not a
substitute for the provider-side precondition, which remains the authoritative
concurrency control.

## TOCTOU protections (summary)

1. Approval binds the *state*, not just the intent: the digest includes the
   expected SHAs.
2. Preconditions are re-read after approval verification and immediately before
   the write.
3. Provider-side conditional execution is used wherever available.
4. Any drift between approval-time state and execution-time state invalidates
   the approval and requires re-approval; it is never auto-refreshed.

## Retry policy

- `create_branch`, `create_pr`: `RETRY_CONDITIONAL` — retry only on transport
  failures where the write-ahead record allows a safe existence probe first.
- `merge_pr`: `NO_RETRY`.
- `429`/`403` rate limiting: honor `Retry-After`, bounded backoff with jitter, a
  global deadline, and never convert a rate-limit into repeated write attempts.
- OD-011 (retry defaults per tier) remains open and must be closed by ADR-0022.
