# Fail-Closed Policy and Audit for Integrations

>
> **V2 · PHASE 7 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Predecessor gates `DIRECT_MUTATION_ACCEPTED`, `BATCH_ACCEPTED`, `DAG_ACCEPTED`
> and `RUNBOOK_ACCEPTED` are not accepted. No provider may be registered, wired
> or health-probed against production credentials on the basis of this lane.

## Canonical ordering (identical for all providers)

```text
1. tool identification and typed schema validation
2. provider allow-list resolution
3. exact target scope check (mailbox / calendar id / entity id / datasource id / repo)
4. policy evaluation (ALLOW / DENY / APPROVAL_REQUIRED) with stable reason code
5. capability readiness check
6. approval + operation-digest binding      [DIRECT_WRITE only]
7. idempotency key resolution / replay check [DIRECT_WRITE only]
8. write-ahead audit record                  [DIRECT_WRITE only]
9. credential resolution (authorization handle)
10. provider execution within budget
11. result normalization, field allow-list, byte budget
12. terminal audit record
```

Any failure short-circuits to a redacted typed refusal. Steps 1–8 must be
provable-by-test to complete **zero** provider calls and, for scope/policy
denial, **zero** credential resolutions — mirroring the accepted Phase 2
evidence pattern.

## Fail-closed defaults

| Condition | Outcome |
|---|---|
| Unknown provider / capability / tool | DENY |
| Capability state not `READY` (write) | DENY |
| Capability `DEGRADED` (read) | ALLOW with degraded marker, recorded |
| Policy engine error or unavailable | DENY (never default-allow) |
| Audit sink unavailable, write path | DENY before side effect |
| Audit sink unavailable, read path | ALLOW read, mark evidence incomplete, alarm; mutation intent refused |
| Budget/deadline exceeded mid-write | Outcome `UNKNOWN`; no automatic retry for non-idempotent classes; manual-intervention state |
| Provider returns unexpected shape | DENY result, `E-PROVIDER-SHAPE` |

## Audit record (per terminal outcome, exactly one)

Fields: request id, principal reference (opaque), provider id, capability id,
tool id, mode, target scope reference, mutation class, idempotency key digest,
operation digest, approval reference, policy decision + reason code, readiness
state, credential capability id + scope-set digest, provider call count, byte
count, duration, outcome class, reason code, evidence digest.

Never present: secrets, tokens, raw request/response bodies, message contents,
attendee personal data beyond opaque references, provider stack traces.

## Reason-code families

`E-POLICY-*`, `E-SCOPE-*`, `E-CAP-*`, `E-CRED-*`, `E-APPROVAL-*`,
`E-IDEMPOTENCY-*`, `E-PROVIDER-*`, `E-AUDIT-*`, `E-BUDGET-*`. Codes are stable,
enumerated and safe to use as bounded metric labels (ADR-0029).
