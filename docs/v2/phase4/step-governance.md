# Per-Step Governance: Policy, Idempotency, Approval, Audit

> **V2 · PHASE 4 · DESIGN ONLY · NOT_IMPLEMENTED · DO_NOT_MERGE UNTIL DIRECT_MUTATION_ACCEPTED**

## Rule: reuse, never re-implement

Every governance decision in a batch is made by the **existing Phase 3
components, unchanged**, once per step:

| Concern | Reused component | Batch layer responsibility |
|---|---|---|
| Policy evaluation | Phase 3 per-operation policy engine | Call it per step; never cache a decision across steps |
| Credential resolution | `github_write_credentials` / read split | Call per step; never hoist a token to batch scope |
| Digest + approval | `mutation_digest` / approval binding | Bind approval to the **step** digest |
| Idempotency | `mutation_idempotency` store | Pass the step's key through untouched |
| Audit | `mutation_audit` | One audit record per step, plus one batch envelope record |
| Redaction | Phase 3 redaction | Applied to every step error and evidence field |

There is **no batch-level authorization**. One request is not one decision.

## Approval binding

- An `approval_ref` is valid only for the digest of *its own step*.
- Presenting a batch-level approval, or reusing one step's approval for another
  step, is a `DENIED` step (and, at validation time, a `DENIED` batch).
- Adding, removing or altering any step changes that step's digest; approvals do
  not survive it.
- The batch digest (over `batch_id` + ordered step digests) exists only for
  evidence and replay detection, never as an authorization token.

## Idempotency across a batch

- Keys are per step. The batch layer never synthesises a key.
- Replaying an identical batch yields `REPLAYED` on steps whose key already
  committed, so a caller-side retry of a partially failed batch is safe.
- A `batch_id` reused with a different batch digest is rejected at validation:
  it signals an unsafe silent mutation of a previously submitted batch.
- Concurrency inside a batch never widens the idempotency window: mutation steps
  are serialised in Phase 4 (`concurrency-and-scheduling.md`).

## Audit records

Per step: the standard Phase 3 mutation/read audit record, additionally carrying
`batch_id` and `step_id` so records can be correlated without joining logs.

Per batch: one envelope record with `batch_id`, batch digest, `effective_parallelism`,
counts per status, start/finish, `evidence_ref`, and the ordered list of step
`audit_ref`s. The envelope record is written even when the batch is `DENIED` at
validation (with zero step records), so a rejected batch is still auditable.

## Dry run

`dry_run=true` performs validation, policy evaluation and digest computation for
every step and returns a `BatchResult` where all steps are `NOT_STARTED` (or
`DENIED` where policy refuses), with `aggregate_status` `SUCCESS` when all steps
would be permitted. It performs **zero** provider calls and writes no
idempotency entries.
