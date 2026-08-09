# Deterministic Failure Injection Catalogue

>
> **V2 · PHASE 9 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires `HYBRID_ACCEPTED` and all Phase 7 integrations in the production cut
> accepted. Nothing here declares production readiness.

Each fault is injected deterministically (hermetic harness) and, where safe,
repeated on the connected host against disposable targets only.

| # | Fault | Expected behaviour |
|---|---|---|
| F-01 | Provider 401/403 | Redacted typed error; capability demoted on repeat; no retry storm |
| F-02 | Provider 429 / rate limit | Backoff within budget for idempotent reads only; refusal for non-idempotent writes |
| F-03 | Provider 5xx | Bounded retry for idempotent classes; `UNKNOWN` outcome for non-idempotent |
| F-04 | Provider timeout mid-write | Outcome `UNKNOWN`, manual-intervention state, no automatic retry, audited |
| F-05 | Provider TLS/DNS failure | Fail closed before credential use where possible |
| F-06 | Provider returns oversized body | Refused before parse |
| F-07 | Provider returns malformed JSON/shape drift | `E-PROVIDER-SHAPE`, no partial acceptance |
| F-08 | Credential broker unavailable | DENY, no cached-secret fallback |
| F-09 | Credential revoked mid-run | In-flight fails closed; capability `UNAVAILABLE` |
| F-10 | Policy engine error | DENY (never default-allow) |
| F-11 | Audit sink unavailable | Write path refused pre-effect; read path degraded + alarm |
| F-12 | Idempotency store unavailable | Non-idempotent writes refused |
| F-13 | Idempotency store returns stale replay | Prior outcome returned; zero second side effect |
| F-14 | Approval store unavailable / approval expired | Refused |
| F-15 | Clock skew / deadline anomaly | Deadlines monotonic-based; skew cannot extend a budget |
| F-16 | Concurrent duplicate request, same digest | Exactly one side effect; second returns prior outcome |
| F-17 | Lease/heartbeat loss (DAG) | Checkpointed resume or dead-letter, never duplicate mutation |
| F-18 | Partial batch failure | Explicit partial-success record, per-node outcomes |
| F-19 | Disk/tmp exhaustion on evidence path | Fail closed on write path; alarm; no silent evidence loss |
| F-20 | Restart mid-operation | On recovery: no duplicate mutation; unknown outcomes surfaced for manual review |

Every fault must produce: a terminal audit record, a stable reason code, and no
secret material anywhere in the resulting artifacts.
