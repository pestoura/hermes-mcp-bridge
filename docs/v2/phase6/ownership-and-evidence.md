# Ownership and Evidence Policy

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

## Ownership

Every admitted runbook has a mandatory, resolvable `owner`:

| Field | Rule |
|---|---|
| `owner.id` | Stable identity reference (role or team identifier), resolvable in the registry at admission; unresolvable → `RB_OWNER_UNRESOLVABLE` |
| `owner.kind` | `role` or `team`; a personal identity alone is not accepted for `MUTATING_HIGH` or `destructive_action = true` |
| `owner.contact` | Operational contact channel |
| `review_cadence_days` | Mandatory; bounded maximum (recommended 180) |
| `last_reviewed_at` | Registry bookkeeping (non-digest) |

Owner responsibilities: correctness of the manifest, currency of the tool
version pins, response to yank/deprecation events, and periodic review.

A runbook whose `last_reviewed_at + review_cadence_days` has elapsed becomes
`REVIEW_OVERDUE`. Overdue is a **signal**, not an automatic yank, except when
`destructive_action = true` or `policy_class = MUTATING_HIGH`, where overdue
review moves the runbook to non-invocable and denies with
`RB_REVIEW_OVERDUE`. Fail-closed applies where the blast radius is largest.

The owner identity is digest-relevant (it participates in accountability for
execution), so an ownership transfer is at least a MINOR version bump.

## Evidence policy

Every runbook execution produces an evidence record, following the Phase 0/1/2
pattern (deterministic collector output + retained, digest-recorded document).

Mandatory fields:

| Field | Notes |
|---|---|
| `execution_id` | Unique, stable |
| `runbook_id`, `version`, `runbook_digest` | Exact triple executed |
| `plan_digest` | Bound plan digest (see `plan-digest-binding.md`) |
| `runbook_snapshot_hash`, `capability_snapshot_hash` | Control-plane state in force (V2-NFR-013) |
| `principal_ref` | Caller identity reference, not credentials |
| `approval_refs[]` | Approval identifiers, approver identity references, consumption timestamps |
| `policy_decisions[]` | Per node: class, decision, reason code |
| `nodes[]` | Per node: tool `(name, version)`, start/end, state, idempotency key, retry count, external call count, provider status codes |
| `write_ahead_records[]` | One per mutating node, written **before** the mutation (inherits A3-05) |
| `destructive_action` | Declared and computed values |
| `rollback` | Attempted, outcome, residual object list |
| `budgets` | Declared vs consumed |
| `token_accounting` | Hermes LLM tokens consumed; **must be 0** on a deterministic runbook |
| `bytes` | Raw vs returned result bytes (V2-NFR-014) |
| `terminal_state` | One of the enumerated terminal states |
| `evidence_digest` | SHA-256 over the canonical evidence bytes |

## Integrity and tamper detection

Evidence is integrity protected and tamper detectable (V2-SEC-023): each record
carries `evidence_digest`, and retained gate documents are indexed with their
SHA-256 in the repository evidence index, exactly as Phases 0–2 do. Write-ahead
audit records are append-only; a mutation with no preceding write-ahead record
is an acceptance failure, not a logging gap.

## Redaction

Redaction fails closed (V2-SEC-008). Never present in evidence, logs, traces,
metric labels or artifacts: tokens, refresh tokens, client secrets, cookies,
authorization headers, private keys, or any parameter marked `sensitive`.
Credentials appear only as capability IDs and status.

A redaction scan is part of the gate; an unscannable field is withheld, not
emitted.

## Retention

Retention defaults remain OD-010. Until resolved, the design requires: a
declared retention period per evidence class, deletion that is recorded rather
than silent, and no retention decision that would remove the evidence backing a
declared gate.

## Metrics

Metrics distinguish execution modes (V2-NFR-003) and add runbook dimensions:
`runbook_id`, `version`, `terminal_state`, `policy_class`,
`destructive_action`. Metric labels are bounded-cardinality and never carry
resource identifiers or user-supplied strings.
