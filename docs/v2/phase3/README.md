# Phase 3 Preparation Lane — GitHub DIRECT Mutations

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Phase 2 gate `DIRECT_READ_ACCEPTED` is **NOT ACCEPTED** at the time of writing.
> Nothing in this lane may be implemented, wired, merged into an executable path
> or treated as acceptance. The operational V1 surface remains exactly
> **27 tools**, bridge `1.0.0`, schema `0.6.1`, contract `1.0.0`.

## Purpose

This lane holds the design work that must exist *before* the first line of
mutation code is written, so that Phase 3 can start immediately after
`DIRECT_READ_ACCEPTED` without inventing security semantics under delivery
pressure. It is deliberately **docs/ADR/contract/test-plan only**.

## Hard preconditions (all must hold before any Phase 3 implementation)

1. `DIRECT_READ_ACCEPTED` declared with `failures=[]` from the connected
   Jarvas/Hermes gate **and** the Phase 2 OUTER gate
   (`../phase2-final-outer-gate.md`).
2. A dedicated least-privilege write credential exists and is separate from the
   `github.read` capability (see `credential-split.md`).
3. The mutation policy set, approval model and idempotency store design are
   accepted (ADR-0020..ADR-0023 promoted from Proposed).
4. The mutation test matrix (`test-matrix.md`) is implemented and failing-closed
   before any provider call is enabled.

## Documents in this lane

| Document | Scope |
|---|---|
| `threat-model-mutations.md` | Mutation-specific threats layered on `../security/threat-model.md` |
| `credential-split.md` | `github.read` / `github.write` / never-`github.admin` separation |
| `mutation-semantics.md` | `github.create_branch`, `github.create_pr` typed contracts |
| `governed-merge.md` | Conditional, governed `github.merge_pr`; repo deletion DENY |
| `idempotency-and-concurrency.md` | Idempotency keys, replay, optimistic concurrency, locks, TOCTOU |
| `approval-and-digest.md` | Operation digest and approval binding for single mutations |
| `audit-and-evidence.md` | Mutation audit record, evidence contract, redaction |
| `acceptance-criteria.md` | Fail-closed `DIRECT_MUTATION_ACCEPTED` criteria |
| `rollback-and-compensation.md` | Per-operation compensation and manual-intervention rules |
| `test-matrix.md` | Required mutation test matrix (positive, negative, adversarial) |

## Reconciliation with existing V2 documents

This lane **does not restate** decisions already recorded elsewhere. It refines
them for the mutation case and points back to the source:

| Existing document | Relationship |
|---|---|
| `../security/threat-model.md` | Base threat set; this lane adds mutation-only threats |
| `../adrs/ADR-0007-least-privilege-credentials.md` (least-privilege credentials) | Base decision; refined by `credential-split.md` + ADR-0020 |
| `../adrs/ADR-0011-per-node-policy.md` (per-node policy) | Applies unchanged; Phase 3 is single-node DIRECT |
| `../adrs/ADR-0012-approval-immutable-plan-digest.md` (approval bound to plan digest) | Base decision; specialized to `operation_digest` in ADR-0021 |
| `../adrs/ADR-0013-idempotency-replay-protection.md` (idempotency/replay) | Base decision; GitHub-specific keys in ADR-0022 |
| `../adrs/ADR-0014-saga-compensation.md` (saga/compensation) | Base decision; per-operation table in `rollback-and-compensation.md` |
| `../architecture/github-direct-read.md` | Read path; mutation path reuses its scope/fail-closed ordering |
| `../requirements/security.md` | V2-SEC-004/005/011/021/025 are the governing requirements |
| `../roadmap.md` Phase 3 | Unchanged; this lane is the detail behind that section |
| `../open-decisions.md` | OD-016 (GitHub credential model) and OD-011 (retry defaults) remain open |

## Non-goals of this lane

- No DIRECT mutation tool is defined in the canonical registry.
- No change to Phase 2 acceptance, gate scripts, collectors or evidence.
- No change to CI gates, contracts (`contracts/1.0.0.json`) or the V1 tool count.
- No credential provisioning, no GitHub App permission change, no live call.
