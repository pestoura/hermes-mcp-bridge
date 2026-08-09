# Fail-Closed Invocation Model

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

## Invocation request

A runbook invocation carries exactly:

`runbook_id`, `version`, `expected_runbook_digest`, `arguments`,
`idempotency_key`, optional `approval_ref` + `expected_plan_digest`, optional
tightened `budgets`, optional `deadline_ms`.

Anything not in this list is rejected. In particular a caller may not supply:
node overrides, tool versions, policy hints, capability names, credentials,
scope widening, `continue_on_error` for mutating nodes, or agentic escalation
permission. Caller input can only **narrow** authority, never widen it.

## Ordered invocation pipeline

Fail-closed; a failure stops the sequence and later steps must be observably not
executed (zero broker calls, zero HTTP requests, zero LLM tokens).

| # | Step | Denial on failure |
|---|---|---|
| 1 | Resolve `(runbook_id, version)`; state must be `ACTIVE` or `DEPRECATED` | `RB_UNKNOWN`, `RB_UNKNOWN_VERSION`, `RB_YANKED`, `RB_NOT_PROMOTED` |
| 2 | Compare `expected_runbook_digest` with the registry digest | `RB_DIGEST_MISMATCH`, `RB_DIGEST_REQUIRED` |
| 3 | Review currency check for high-blast-radius runbooks | `RB_REVIEW_OVERDUE` |
| 4 | Validate arguments against the closed parameter schema | `RB_SCHEMA_INVALID` |
| 5 | Resolve and intersect resource scope (runbook ∩ caller ∩ policy) | `RB_SCOPE_DENIED` |
| 6 | Evaluate policy: runbook aggregate class and each node's class | `RB_POLICY_MISSING`, `RB_POLICY_DENIED` |
| 7 | Compute `plan_digest` from the resolved plan | — |
| 8 | Capability readiness probe against `min_capability_state` | `RB_CAPABILITY_NOT_READY`, `RB_INSUFFICIENT_CAPABILITY`, `RB_CAPABILITY_DRIFT` |
| 9 | Idempotency check: existing terminal execution for this key + digest → return the recorded result | `RB_IDEMPOTENCY_CONFLICT` when the key matches but the digest differs |
| 10 | Approval: verify binding, expiry, nonce, approver distinctness; consume atomically | `RB_APPROVAL_*` |
| 11 | Acquire execution lease | `RB_LEASE_UNAVAILABLE` |
| 12 | Per mutating node: write-ahead audit record, then credential resolution, then provider call | `RB_AUDIT_WRITE_FAILED` (deny — no mutation without a write-ahead record) |
| 13 | Shape result against the closed output schema; redact fail-closed | `RB_REDACTION_UNPROVEN` |
| 14 | Emit evidence record with both digests and snapshot hashes | — |

Credential resolution occurs at step 12 and nowhere earlier. A denial at steps
1–11 must leave a provably clean footprint.

## Least privilege at invocation

- Only the capabilities computed at admission are resolvable; the engine cannot
  widen them at runtime.
- Each node receives only its own credentials, for its own duration.
- Projection (ADR-0005) hides runbooks the caller is not authorized to invoke;
  an unauthorized caller receives `RB_UNKNOWN`, not `RB_POLICY_DENIED`, so the
  registry does not leak the existence of restricted runbooks.
- Runbook metadata never expands authority (V2-SEC-013).

## Determinism

A deterministic runbook consumes **zero Hermes LLM tokens**. Token accounting
is measured from real runtime accounting and asserted at the gate; a non-zero
count on a deterministic runbook is an acceptance failure, not a rounding
detail.

## Idempotency

The execution-level idempotency key is `(idempotency_key, plan_digest)`. A
repeat with the same pair returns the recorded terminal result and performs
zero additional provider mutations. The same key with a different digest is a
conflict, never a new execution under the old key. Node-level idempotency
(Phase 3, ADR-0022) continues to apply independently.

## Concurrency

Executions of the same runbook against the same resource scope are serialized
by a lock or optimistic concurrency check appropriate to the resource. Lease
loss mid-execution suspends progress; recovery either resumes from checkpoint
or dead-letters — it never re-executes a committed mutating node.

## Denial code discipline

Reason codes are stable, enumerable, machine-checkable and safe to log. They
carry no secret material and no user-supplied strings. Every denial code used
in this lane appears in the test plan and maps to at least one acceptance
criterion.
