# Rollback and Rotation Drills

>
> **V2 · PHASE 9 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires `HYBRID_ACCEPTED` and all Phase 7 integrations in the production cut
> accepted. Nothing here declares production readiness.

## Rollback drill

| Step | Requirement |
|---|---|
| Preconditions | Previous accepted artifact identified by immutable digest; its evidence still retained |
| Trigger | Simulated production defect; drill is announced and timed |
| Execution | Roll back to the previous accepted artifact using the documented procedure only — no ad-hoc edits |
| Target | Complete within 15 minutes, measured |
| Verification | Health probes green; V1 surface exactly 27 tools; capability states as expected; zero duplicate mutations; audit continuity across the switch |
| In-flight work | Either completed or failed closed; no partially applied write left unrecorded |
| Evidence | Timed log, before/after digests, verification output, sanitized |

## Layered rollback options (must all be documented and at least the first two drilled)

1. Disable a single provider by allow-list removal (`E-PROVIDER-UNKNOWN`).
2. Disable HYBRID and return to the prior accepted deterministic behaviour.
3. Disable the V2 execution path entirely and fall back to the V1 agentic path.
4. Roll back the artifact to the previous accepted release.

## Credential rotation drill

Rotate each production credential domain; verify: no failed-open, in-flight
requests complete on the old handle or fail closed, capability returns `READY`
after rotation without restart, audit shows the rotation, and no secret material
appears in any artifact.

## Restore drill

Restore the audit/evidence store from backup into an isolated location and verify
the digest chain reproduces; record the verification, not just the backup's
existence.
