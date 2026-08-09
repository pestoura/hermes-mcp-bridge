# Phase 3 Governed Merge and Destructive-Operation Policy

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

## `github.merge_pr` — conditional and governed

Merge is the highest-risk operation in Phase 3 because merged content can
execute privileged CI. It is **not** part of the first mutation increment and
must be gated separately from `create_branch` / `create_pr`.

### Required preconditions (all, re-read immediately before the call)

1. Repository is in the exact allow-list **and** explicitly listed as
   merge-enabled in policy (a separate list from the write allow-list).
2. `base` is **not** the repository default branch unless the repository is
   explicitly marked `default_branch_merge_allowed` in policy. Default: DENY.
3. PR state is `open`, `mergeable == true`, `mergeable_state` in an explicit
   allow-list, and not a draft.
4. All required status checks for the base branch are `success`. Absence of
   required checks is a DENY, not a pass.
5. Required reviews are satisfied according to the live branch protection state,
   read at execution time.
6. `expected_head_sha` supplied by the approver still matches the PR head.
7. A valid, unexpired, single-use **human** approval bound to the
   `operation_digest` that includes the head SHA.

Any precondition that cannot be evaluated (API error, unknown field, missing
protection data) is a DENY.

### Execution constraints

- REST: `PUT /repos/{owner}/{repo}/pulls/{number}/merge` with `sha` set to
  `expected_head_sha` so GitHub itself enforces the optimistic-concurrency check
  (server-side `409` on head movement).
- Merge method is policy-fixed per repository (`merge`, `squash` or `rebase`);
  the caller cannot choose it.
- No auto-merge, no merge queue manipulation, no admin bypass
  (`administrators` enforcement must not be circumvented).
- Idempotency class: `NO_RETRY`. A timeout or ambiguous response is resolved by
  **reading** the PR state, never by re-issuing the merge.
- Compensation: none automatic. A merged commit is not auto-reverted; the
  operation dead-letters to manual intervention with full evidence.

## Repository deletion — permanent DENY

`github.delete_repo` and every equivalent destructive administration operation
are **DENY by default and out of scope for V2**:

- not defined in the canonical registry;
- not reachable through any typed tool;
- the `Administration` permission is never granted to any V2 capability, so the
  operation is *unrepresentable*, not merely policy-blocked;
- a policy DENY rule is additionally recorded as defense in depth, and a test
  asserts that no registry entry, no permission set and no code path can produce
  a `DELETE /repos/...` request.

The same treatment applies to: deleting branches other than an in-chain
compensation ref, force-pushing, rewriting history, changing branch protection,
managing webhooks/deploy keys/secrets, and any organization-level operation.
