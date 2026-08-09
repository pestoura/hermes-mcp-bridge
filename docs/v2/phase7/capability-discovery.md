# Direct-Read / Direct-Write Capability Discovery

>
> **V2 · PHASE 7 · implemented, disabled by default behind `PROVIDER_FEATURE_ENABLED`**
>
> Predecessor gates `DIRECT_MUTATION_ACCEPTED`, `BATCH_ACCEPTED`, `DAG_ACCEPTED`
> and `RUNBOOK_ACCEPTED` are accepted. Only `github` and `jira` are in the
> provider allow-list; every other provider stays `CANDIDATE` or
> `BLOCKED_UNCONFIRMED` and is refused at registration.

## Model

Discovery is **declarative first, probe second, demote only**.

1. **Declare.** The provider manifest lists capability ids with their class
   (`DIRECT_READ` / `DIRECT_WRITE`), required credential capability, required
   scopes, egress hosts, byte/deadline budgets and mutation/idempotency classes.
2. **Validate.** The gateway rejects a manifest that declares a capability with
   no registered typed tool, an unknown credential capability, a scope broader
   than the credential domain allows, or a duplicate id.
3. **Probe.** `health()` runs against the authorized target set using status-only
   credential information. Probes are read-only; a `DIRECT_WRITE` capability is
   probed by a read that proves scope, never by a test mutation.
4. **Classify readiness.** Reuse the accepted Phase 1 seven-state model. A probe
   may move a capability only downward: `READY -> DEGRADED -> UNAVAILABLE`.
   A probe can never create, promote or widen a capability.

## Direct-read vs direct-write discovery differences

| Aspect | DIRECT_READ | DIRECT_WRITE |
|---|---|---|
| Probe method | Bounded read against an authorized target | Read-only scope assertion; never a trial write |
| Default state when probe inconclusive | `DEGRADED` (may serve with warning) | `UNAVAILABLE` (fail closed) |
| Credential | `<provider>.read` | `<provider>.write`, distinct domain |
| Policy default | ALLOW within scope | APPROVAL_REQUIRED or DENY per tier |
| Snapshot effect | Included in capability snapshot hash | Included, plus a separate `write_capability_digest` so a write surface change is individually detectable |

## Snapshot determinism

Discovery output is canonically serialized (sorted keys, no free text, no
timestamps in the hashed body) and hashed, extending the accepted Phase 1
snapshot. Any change to the exposed write surface changes
`write_capability_digest`; acceptance evidence records both digests.

## Refusal codes

`E-CAP-UNDECLARED`, `E-CAP-DUPLICATE`, `E-CAP-SCOPE-EXCEEDS-CREDENTIAL`,
`E-CAP-PROBE-INCONCLUSIVE`, `E-CAP-NOT-READY`, `E-CAP-WRITE-DISCOVERY-DENIED`.
