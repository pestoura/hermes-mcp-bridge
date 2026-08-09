# V2 Phase 9 — Operational Runbooks

> **V2 · PHASE 9 · production hardening**

One runbook per failure class in the accepted catalogue. Each is written for an
on-call operator with no prior context on the incident: what they will see, what
it means, what to do, and how to know it worked.

Two rules apply to every runbook here and are not repeated in each section:

* **Never widen a control to clear an alert.** Disabling a gate, granting a
  broader credential or skipping an approval to make an alert stop is an
  incident of its own.
* **Never paste credential material, tokens, raw environment, request bodies or
  provider responses into a ticket, chat or postmortem.** Reference audit
  records by `request_id` and digest instead.

## Index

| Runbook | Trigger |
|---|---|
| [R1 Lifecycle / draining](#r1-lifecycle-and-draining-remediation) | Drain never reaches zero; restart appears to hang |
| [R2 Rollback](#r2-rollback) | A promoted build must be withdrawn |
| [R3 Credential rotation](#r3-credential-rotation) | Scheduled rotation, suspected exposure, revoked upstream token |
| [R4 Restore](#r4-restore) | State or audit loss; recovery from backup |
| [R5 Audit recovery](#r5-audit-recovery) | Audit sink outage, chain break, completeness < 100% |
| [R6 Unknown outcome / manual intervention](#r6-unknown-outcome-and-manual-intervention) | A write ended `UNKNOWN` |
| [R7 Provider degradation](#r7-provider-degradation) | One or more capabilities not `READY` |
| [R8 Policy / approval refusals](#r8-policy-and-approval-refusals) | Sudden rise in refusals |

---

## R1 Lifecycle and draining remediation

**Symptom.** A shutdown or restart does not complete. The drain reports live
work that never decreases, and the process appears to hang rather than exit.

**Meaning.** The in-flight registry is counting an operation that has already
finished — a *ghost*. The accepted root cause is a registration that is not
cleared on every exit path; the fix is an unconditional clear in the `finally`
block, so the count drops even when the turn raises or is cancelled.

**Diagnosis.**

1. Read the drain summary: `admitted`, `completed`, `live_after_grace`,
   `survivors_after_sweep`, `manual_restart_required`.
2. `admitted == completed` while `live_after_grace > 0` is the ghost signature:
   the work finished, the bookkeeping did not.
3. `admitted > completed` is *not* a ghost — that is genuinely slow work, and
   the correct action is to wait for the grace window.

**Action.**

1. Let the bounded drain run to the end of its grace window. It sweeps
   survivors; a healthy system reaches `survivors_after_sweep == 0` without
   help.
2. If `manual_restart_required` is true, restart the process. This is a
   supported outcome, not a failure — the drain is bounded precisely so an
   operator is never left waiting indefinitely.
3. Confirm the regression test `test_gateway_finally_clears_shutdown_registry`
   is present and passing on the deployed revision. If it is absent, the deployed
   build predates the fix and must be rolled forward, not patched in place.

**Verification.** A subsequent restart drains to zero with
`manual_restart_required` false.

**Escalate when.** Ghosts reappear on a revision where the regression test
passes — that is a new defect, and the drain summary plus the revision should go
to the maintainer.

---

## R2 Rollback

**Trigger.** A promoted build must be withdrawn: failed acceptance, a
regression in production, or a security finding in the running image.

**Preconditions.** The exact rollback image and its immutable image ID are
known and recorded. Rolling back to a tag is not permitted — tags move.

**Action.**

1. Decide the rollback class:
   * **Option 1 — provider withdrawal.** Remove the provider from the allow-list.
     Narrowest possible action; other capabilities keep serving. Prefer this
     whenever the fault is confined to one provider.
   * **Option 2 — image rollback.** Redeploy the previously accepted image by
     digest.
2. Run the rollback in **dry-run first**. It is dry-run by default and requires
   an explicit `REQUIRED_SHA`; that requirement exists so a rollback cannot be
   fired at the wrong revision by accident.
3. Review the dry-run output: compose project, target image ID, expected bridge
   version and tool count.
4. Execute. Wait for the health check to stabilize before declaring success.
5. Confirm the tool count and bridge/schema versions match the rollback target.

**Verification.** Health stable; version and tool count as expected; drain from
the previous build reached zero (see R1).

**RTO.** 15 minutes for the full drill, measured. The executable drill
(`run_rollback_drill`) asserts this bound in CI.

**Do not.** Revert the schema. Rollback is image-level; state migrations are not
reversed by this procedure.

---

## R3 Credential rotation

**Trigger.** Scheduled rotation, suspected exposure, or an upstream revocation.

**Key property.** Rotation does **not** require a restart. Material is replaced
in the broker; handles already minted keep working against their own material
until spent or expired, and are never silently retried on the new material.

**Action.**

1. Provision the new material into its file-backed location with the same
   ownership and `0600` permissions as the existing one. Never echo the value,
   never place it in shell history, never attach it to a ticket.
2. Rotate the domain. Confirm the capability reports ready afterwards.
3. Watch for a burst of `E-CRED-UNAVAILABLE` or `E-CRED-REVOKED`. A handful
   during the switchover is expected and is fail-*closed* behaviour. A sustained
   stream means the new material is wrong or under-scoped.
4. Revoke the old material upstream only after the new one is confirmed serving.

**Verification.** Reads succeed on the new material; no failed-open (a refusal
is acceptable, an unauthenticated success is not); scope is unchanged — rotation
must never widen scope, and `test_p9_d09_rotation_never_widens_scope` asserts it.

**If exposure is suspected.** Revoke first and accept the outage. A revoked
domain fails closed by design; that is the correct state while you provision a
replacement.

---

## R4 Restore

**Trigger.** State or audit data loss, or a recovery rehearsal.

**Objectives.** Gateway RTO ≤ 5 minutes (manual restart acceptable). Audit RPO
is **0 terminal records for write operations** — any loss is a failure, not a
tolerance.

**Action.**

1. Stop accepting new writes before restoring. Restoring underneath live traffic
   invalidates the chain comparison you are about to make.
2. Restore from the most recent verified backup.
3. Recompute the audit chain digest over the restored records and compare it to
   the pre-loss digest. Equality is the acceptance criterion: it proves nothing
   was lost, reordered or altered. A record count that matches while the digest
   differs means tampering or reordering, not partial loss.
4. Reconcile any `UNKNOWN` outcomes recorded before the loss (see R6) *before*
   reopening the write path.
5. Reopen writes.

**Verification.** Chain digest matches; completeness is 1.0; zero unresolved
unknowns.

**Never.** Reopen the write path with an unverified chain. A silent gap in the
audit record is worse than an extended outage.

---

## R5 Audit recovery

**Symptom.** Audit sink unavailable, chain verification fails, or completeness
is below 100%.

**Expected system behaviour.** The write path is *refused* while the sink is
unavailable — `E-AUDIT-UNAVAILABLE`, before any side effect. Reads may continue
in a degraded, explicitly marked state. This is by design: an unaudited write is
not permitted, so a sink outage presents as refused writes rather than silent
ones.

**Action.**

1. Confirm no side effect occurred during the outage: refusals carry zero
   provider calls, so a refused write did not reach the provider.
2. Restore sink availability.
3. Verify the chain from genesis. A break localizes to the first link whose
   recomputed digest differs from its successor's `prev_digest`.
4. If the break is genuine tampering, preserve the artifacts and escalate as a
   security incident — do not repair the chain in place.
5. Reconcile completeness: terminal records ÷ terminal outcomes must be 1.0.

**Verification.** Writes accepted again; chain verifies; completeness 1.0.

---

## R6 Unknown outcome and manual intervention

**Symptom.** A write ended with outcome `UNKNOWN` — typically a timeout or a
transport fault after the request left the gateway.

**Meaning.** The provider state is genuinely unknown. The mutation may or may
not have been applied. This is neither a success nor a failure, and it is the one
state where automation must stop.

**Action.**

1. **Do not retry.** A blind retry is the single fastest way to create a
   duplicate mutation. The idempotency store enforces this: an `AMBIGUOUS`
   record refuses new attempts with `RECONCILIATION_REQUIRED`.
2. Read the provider's actual state for the operation target — did the PR, the
   branch, the issue get created?
3. Resolve deliberately:
   * **It was applied** → record the mutation as committed; no further action.
   * **It was not applied** → mark the record clean so exactly one fresh attempt
     is permitted.
4. Record which way you resolved it and on what evidence.

**Verification.** No `AMBIGUOUS` records remain; provider state matches the
recorded outcome; the provider call count for the operation is exactly 1.

**Never.** Clear an unknown by deleting the record. That removes the only thing
preventing a duplicate.

---

## R7 Provider degradation

**Symptom.** A capability reports `DEGRADED` or `UNAVAILABLE`; refusals carry
`E-CAP-NOT-READY`.

**Expected behaviour.** Degradation is scoped to the affected provider. Other
providers keep serving — no cascade. Reads may serve `DEGRADED` with an explicit
marker on the result; writes require `READY` and are refused otherwise.

**Action.**

1. Identify scope: one capability, one provider, or several. Several
   simultaneously still refuse cleanly rather than cascading.
2. If the provider is down upstream, withdraw it (R2 option 1) rather than
   leaving requests to fail one at a time.
3. Do not promote a capability manually to clear the alert. Promotion out of
   `CONFIGURED` happens once; probes only ever demote. Overriding this hides the
   fault.

**Verification.** Unaffected capabilities still succeed; affected ones refuse
with a stable reason code; no write executed against a non-`READY` capability.

---

## R8 Policy and approval refusals

**Symptom.** A rise in `E-POLICY-DENY`, `E-APPROVAL-MISSING`,
`E-APPROVAL-DIGEST-MISMATCH` or `E-SCOPE-DENY`.

**Triage.**

| Reason code | Most likely cause | Action |
|---|---|---|
| `E-POLICY-UNAVAILABLE` | Policy engine unreachable | Restore it. Refusal is correct; a policy outage must never default-allow. |
| `E-POLICY-DENY` | Policy genuinely denies | Confirm intent with the requester. Change policy deliberately, never ad hoc. |
| `E-APPROVAL-MISSING` | No approval, or already consumed | Approvals are single-use. Issue a new one for the new attempt. |
| `E-APPROVAL-DIGEST-MISMATCH` | The operation changed after approval | **Treat as adversarial until proven otherwise.** The approved plan and the executed plan differ. |
| `E-SCOPE-DENY` | Target outside the exact allow-list | Add the target explicitly if legitimate. Wildcards are not available and must not be requested. |

**Never.** Widen scope, disable approval or relax policy to clear a refusal
during an incident. Each of those converts a working control into an outage of
its own.
