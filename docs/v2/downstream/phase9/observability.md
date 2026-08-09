# Observability with Bounded Label Cardinality

>
> **V2 · PHASE 9 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires `HYBRID_ACCEPTED` and all Phase 7 integrations in the production cut
> accepted. Nothing here declares production readiness.

## Label contract (ADR-0029)

Allowed labels are drawn from closed enumerations only:

| Label | Domain |
|---|---|
| `provider` | registered provider ids (~10) |
| `capability` | registered capability ids (bounded by registry) |
| `mode` | `DIRECT`,`BATCH`,`DAG`,`RUNBOOK`,`AGENTIC`,`REFUSED` |
| `outcome` | `success`,`partial`,`refused`,`error`,`unknown` |
| `reason_code` | closed `R-`/`E-` enumeration |
| `tier` | `T1`,`T2`,`T3` |

**Forbidden as labels:** repository/branch names, email addresses, entity ids,
user ids, request ids, paths, queries, free text, error messages, provider
response fragments. These belong to audit records and exemplars.

Cardinality budget: total active series attributable to V2 ≤ a declared ceiling
(proposed 5,000); a cardinality test asserts the ceiling under adversarial input,
proving unbounded values cannot enter labels.

## Minimum metric set

- `v2_requests_total{provider,capability,mode,outcome,reason_code}`
- `v2_request_duration_seconds` histogram `{provider,capability,mode}`
- `v2_provider_calls_total{provider,outcome}`
- `v2_tokens_total{mode,kind}` where `kind ∈ input|output|cache|reasoning`
- `v2_escalations_total{reason_code}`
- `v2_capability_state{provider,capability,state}`
- `v2_audit_records_total{outcome}` and `v2_audit_failures_total{reason_code}`
- `v2_approval_events_total{outcome}`
- `v2_idempotency_replays_total{provider}`
- `v2_budget_exhausted_total{kind}`

## Alerts (proposed)

Audit failure rate > 0; write refusals due to sink/broker outage > 0;
capability `UNAVAILABLE` for a production capability; unknown-outcome count > 0;
cardinality ceiling at 80%; error-budget burn.

Traces/exemplars may carry high-cardinality context, and are subject to the same
redaction rules.
