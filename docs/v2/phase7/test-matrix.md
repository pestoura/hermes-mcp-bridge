# Phase 7 Required Test Matrix

>
> **V2 · PHASE 7 · implemented, disabled by default behind `PROVIDER_FEATURE_ENABLED`**
>
> Predecessor gates `DIRECT_MUTATION_ACCEPTED`, `BATCH_ACCEPTED`, `DAG_ACCEPTED`
> and `RUNBOOK_ACCEPTED` are accepted. Only `github` and `jira` are in the
> provider allow-list; every other provider stays `CANDIDATE` or
> `BLOCKED_UNCONFIRMED` and is refused at registration.

Per provider, all rows are required before that provider's gate. Tests are
hermetic (no live provider) except the explicitly connected acceptance run.

| # | Class | Test intent | Expected |
|---|---|---|---|
| P7-01 | positive | Declared read capability executes within scope | Typed result, allow-listed fields only |
| P7-02 | positive | Result exceeds byte budget | `E-PROVIDER-RESULT-TOO-LARGE`, no partial leak |
| P7-03 | negative | Target outside exact scope | DENY, zero provider calls, zero credential resolutions |
| P7-04 | negative | Policy DENY | DENY, zero provider calls |
| P7-05 | negative | Capability not READY (write) | DENY before credential resolution |
| P7-06 | negative | Missing/expired approval on T3 | `E-APPROVAL-*`, no side effect |
| P7-07 | negative | Replayed idempotency key | Prior outcome returned, no second side effect |
| P7-08 | negative | Audit sink unavailable on write | Refused before side effect |
| P7-09 | adversarial | Cross-domain credential request | `E-CRED-CROSS-DOMAIN`, audited |
| P7-10 | adversarial | Manifest declares scope wider than credential | Load-time refusal |
| P7-11 | adversarial | Free-text/query injection into typed filters | Refused; qualifiers built by code |
| P7-12 | adversarial | Provider returns redirect / unexpected host | Refused, no follow |
| P7-13 | adversarial | Provider returns malformed or oversized JSON | `E-PROVIDER-SHAPE`, redacted |
| P7-14 | adversarial | Secret-looking value in manifest/metadata | Rejected by audit-safe serialization |
| P7-15 | adversarial | Instruction-like content inside provider data (email body, event title) | Treated as data; no execution influence |
| P7-16 | isolation | Provider raises unhandled exception | Contained, other providers stay READY |
| P7-17 | isolation | Provider exceeds deadline | `E-PROVIDER-DEADLINE`; non-idempotent write marked UNKNOWN, no retry |
| P7-18 | determinism | Capability snapshot + write digest stable across runs | Byte-identical |
| P7-19 | regression | V1 surface unchanged | Exactly 27 tools |
| P7-20 | redaction | Full audit/evidence corpus scanned | Zero secret material, zero raw bodies |
