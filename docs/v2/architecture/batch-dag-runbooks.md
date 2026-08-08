# Batch, DAG and Runbook Execution

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

## BATCH scheduler

Independent operations are transported in one bridge request and scheduled through a bounded worker pool. Limits exist globally, per request, per provider, per resource and per credential. The scheduler must provide fair scheduling, backpressure, queues, provider-aware rate limiting, circuit breakers and adaptive concurrency after 429/rate-limit responses.

Request budgets may include `max_nodes`, `max_external_calls`, `max_parallelism`, `max_runtime_ms`, `max_result_bytes`, `max_artifacts`, `max_agentic_escalations`, `max_agentic_tokens`, `max_retries`.

## DAG scheduler

DAG nodes declare typed inputs and explicit dependencies. The engine must:

- validate schemas before execution;
- detect cycles;
- build a deterministic topological order;
- execute independent branches in parallel within bounds;
- support typed output-to-input bindings;
- propagate cancellation and deadlines;
- checkpoint/resume long executions;
- support leases/heartbeat/recovery;
- place unrecoverable executions into `MANUAL_INTERVENTION_REQUIRED` dead-letter state.

No arbitrary `eval`, shell interpolation or unsafe expression language is permitted for bindings.

## Failure semantics

Supported terminal/aggregate states: `SUCCESS`, `FAILED`, `PARTIAL_SUCCESS`, `CANCELLED`, `TIMED_OUT`, `COMPENSATED`, `MANUAL_INTERVENTION_REQUIRED`.

`fail_fast` and `continue_on_error` must be constrained by operation semantics; mutations cannot blindly continue merely because a caller selected a permissive mode.

## Runbooks

A runbook is executable, deterministic, typed, versioned, validated, testable, auditable and governed. A skill is LLM-oriented knowledge/procedure. Stable procedures may follow an explicit `Skill -> Promote -> Runbook` process after validation, tests, threat review and integrity controls.

Promoted runbooks should be canonically serialized/compiled and optionally signed/digested before production use.
