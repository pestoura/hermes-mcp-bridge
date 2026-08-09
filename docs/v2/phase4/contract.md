# BATCH Typed Contract

> **V2 · PHASE 4 · DESIGN · unblocked by `DIRECT_MUTATION_ACCEPTED` (a86b26d) · runtime gated behind `BATCH_FEATURE_ENABLED` until `BATCH_ACCEPTED`**

## Principle

A batch is a **typed envelope over N already-typed DIRECT steps**. It adds no
new capability. Any operation that cannot be expressed as an existing typed
registry entry cannot appear in a batch. There is no free-form command,
no shell, no generic HTTP (see `non-goals.md`).

## BatchRequest

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | `str` | yes | Must equal the batch schema constant; unknown value → reject |
| `mode` | `Literal["BATCH"]` | yes | Discriminator |
| `batch_id` | `str` | yes | Caller-supplied, unique per logical batch; reused value with a different canonical digest → reject |
| `steps` | `Sequence[BatchStep]` | yes | `1 <= len(steps) <= max_items` |
| `failure_policy` | `Literal["fail_fast", "continue_on_error"]` | yes | No default; explicit choice required |
| `max_parallelism` | `int` | yes | Clamped to the server ceiling, never raised by the caller |
| `batch_timeout_s` | `int` | yes | Wall-clock ceiling for the whole batch |
| `dry_run` | `bool` | yes | `true` performs validation + policy evaluation only |

Unknown top-level fields are rejected (fail-closed, no silent drop).

## BatchStep

| Field | Type | Required | Notes |
|---|---|---|---|
| `step_id` | `str` | yes | Unique inside the batch; stable identity in results and audit |
| `tool` | `str` | yes | Must resolve in the typed registry; mutation tools only when Phase 3 allows them |
| `args` | `Mapping[str, object]` | yes | Validated by the tool's own typed schema, unchanged |
| `idempotency_key` | `str \| None` | yes (nullable) | Passed straight to the Phase 3 idempotency store |
| `approval_ref` | `str \| None` | yes (nullable) | Bound to the *step* digest, never to the batch digest |
| `step_timeout_s` | `int` | yes | Must be `<= batch_timeout_s` |
| `depends_on` | `Sequence[str]` | yes | Empty for independent steps. Phase 4 accepts only the empty sequence; non-empty is reserved for the DAG mode and rejected |

`depends_on` exists in the type from day one so that the DAG mode does not
require a breaking contract change; Phase 4 validates it as empty.

## BatchStepResult

| Field | Type | Notes |
|---|---|---|
| `step_id` | `str` | Mirrors the request |
| `status` | `Literal["SUCCESS","FAILED","DENIED","TIMED_OUT","CANCELLED","NOT_STARTED"]` | Exhaustive; no `UNKNOWN` |
| `started_at` / `finished_at` | `str \| None` | RFC3339 UTC; `None` when `NOT_STARTED` |
| `result` | `Mapping[str, object] \| None` | Shaped exactly as the DIRECT result for that tool |
| `error` | `Mapping[str, object] \| None` | Typed error code + redacted message; never a raw provider payload |
| `idempotency_outcome` | `Literal["EXECUTED","REPLAYED","CONFLICT"] \| None` | From the Phase 3 store |
| `audit_ref` | `str \| None` | Phase 3 audit record identifier |

## BatchResult

| Field | Type | Notes |
|---|---|---|
| `batch_id` | `str` | Mirrors the request |
| `aggregate_status` | `Literal["SUCCESS","PARTIAL","FAILED","DENIED","TIMED_OUT","CANCELLED"]` | Algebra in `aggregation-and-evidence.md` |
| `steps` | `Sequence[BatchStepResult]` | One entry per requested step, always, including `NOT_STARTED` |
| `counts` | `Mapping[str, int]` | Per-status totals; must sum to `len(steps)` |
| `started_at` / `finished_at` | `str` | RFC3339 UTC |
| `evidence_ref` | `str` | Aggregation evidence record |

## Invariants

1. `len(result.steps) == len(request.steps)` for every terminal outcome.
2. `sum(counts.values()) == len(request.steps)`.
3. A step never appears twice; `step_id` is the join key everywhere.
4. One request is **not** one authorization decision: each step carries its own
   policy, credential, scope, quota and audit decision.
5. The batch envelope never widens a step's permissions; it may only narrow
   them (budget exhaustion → `NOT_STARTED`/`CANCELLED`).
