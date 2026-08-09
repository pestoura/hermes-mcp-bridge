# Phase 3 Rollback and Compensation

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

Specializes ADR-0014. Compensation is itself a privileged mutation and is
governed by the same policy, approval, idempotency and audit rules as the
forward operation.

| Forward operation | Compensatable | Compensation | Safety conditions | If conditions fail |
|---|---|---|---|---|
| `github.create_branch` | Yes | `DELETE /git/refs/heads/{branch}` | Ref still points at exactly the SHA this operation created; no open PR references it; branch is not protected | Manual intervention |
| `github.create_pr` | Yes | Close the PR (never delete) | PR still open, still at the same head SHA, has no merge commit | Manual intervention |
| `github.merge_pr` | **No** | None automatic | — | Always manual intervention with full evidence |

## Rules

1. **No automatic revert of merged history.** A revert is a new, separately
   approved forward operation, not a rollback.
2. **Ordered compensation.** In a create-branch→create-pr chain, compensation
   runs in reverse order: close the PR first, then delete the branch, and only
   if the branch was created by the same chain.
3. **Compensation is not free authority.** It requires its own policy decision.
   A principal allowed to create is not automatically allowed to compensate a
   different principal's operation.
4. **Unsafe compensation dead-letters.** If any safety condition is unverifiable,
   the operation moves to a manual-intervention state with the audit record
   marked `COMPENSATION_UNSAFE`. It is never attempted "best effort".
5. **AMBIGUOUS outcomes are reconciled by reading, not by compensating.** The
   provider state is read first; compensation is only considered once the real
   state is known.
6. **Evidence must show both states.** Acceptance requires evidence of the
   committed state and the compensated state, including the residual object
   count after cleanup (the Phase 0 baseline used the same residual-count
   discipline).
