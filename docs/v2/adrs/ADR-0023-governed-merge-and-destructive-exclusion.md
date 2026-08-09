# ADR-0023 — Governed Merge and Destructive-Operation Exclusion

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

**Status:** Proposed

## Context
Merging changes what CI executes with repository privileges, and repository
deletion is irreversible. Both need a stronger control than "policy says no".

## Decision
`github.merge_pr` is a separate, later increment from `create_branch` /
`create_pr`. It requires: an explicit merge-enabled repository list, DENY on the
default branch unless explicitly enabled, all required checks green, live branch
protection satisfied, a distinct human approver, an `expected_head_sha` pushed
to the provider as the `sha` parameter, a policy-fixed merge method, `NO_RETRY`
and no automatic compensation. Repository deletion and all administrative
mutations are excluded from V2 by *capability*, not only by policy.

## Consequences
Merge automation is deliberately slow and narrow. Some legitimate merges will
require human action; that is accepted.

## Alternatives
Treat merge as one more mutation under the generic model; allow admin-bypass
merges; rely on policy alone to block deletion.

## Security implications
Addresses T3-05, T3-06 and T3-10. Excluding the `Administration` permission
makes the worst-case operation unreachable rather than conditionally blocked.

## Operational implications
Branch protection and required-check configuration become part of the acceptance
surface; unreadable protection state is a DENY.

## Open questions
Whether a merge queue integration is ever in scope, and whether revert should be
offered as an explicit forward operation in a later phase.
