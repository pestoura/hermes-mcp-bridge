# Phase 4 promotion — `BATCH_ACCEPTED`

Machine-checked promotion of the Phase 4 BATCH engine.

## Gate runner

```
python scripts/validate_v2_phase4_batch_gate.py \
    --json-out docs/v2/evidence/phase4-batch-acceptance.json
```

Exit code `0` only when `failures == []`.

* INNER — V1 contract invariants; the canonical limit constants; the feature
  flag defaulting to off; the presence of a real test for every scenario
  `S-01..S-27` (a missing scenario is a failure, not a skip); a real run of the
  acceptance suite where any reported skip/xfail is a failure.
* OUTER — SHA-256 binding of every Phase 4 module; an AST scan proving zero
  generic surface (no `subprocess`, `socket`, `requests`/`httpx`/`urllib`/`http`,
  no `eval`/`exec`/`compile`/`__import__`/`system`/`popen`); the
  `DIRECT_MUTATION_ACCEPTED` marker proving Phase 3 preceded Phase 4.

## Real concurrency measurement

The gate does not trust the code comment: it runs a live batch of 4 independent
read steps at `max_parallelism=2` against an `asyncio.Barrier(2)` executor and
records `max_observed_inflight`. A serial implementation cannot release the
barrier and fails on timeout, so `max_observed_inflight >= 2` is proof of real
parallel execution.

## Canonical limits

| Constant | Value |
| --- | --- |
| `BATCH_MAX_ITEMS` | 10 |
| `BATCH_MAX_PARALLELISM` | 4 |
| `BATCH_MAX_PARALLELISM_MUTATION` | 1 |
| `BATCH_MAX_TIMEOUT_S` | 300 |
| `BATCH_MAX_INFLIGHT_GLOBAL` | 8 |

Caller values above a ceiling are `DENIED` at validation, never silently
clamped. Backpressure is two bounded semaphores (per batch, process-global);
there is no unbounded queue and no unbounded task spawning.

## Semantics summary

* Results are always in input order; completion order is undefined.
* `fail_fast` and `continue_on_error` are explicit — there is no default.
* `PARTIAL` is a first-class aggregate status.
* Cancellation closes admission **before** cancelling in-flight steps; a step
  cancelled mid-mutation is recorded as indeterminate at the bridge. No
  rollback, no compensation, no implicit retries.
* Per-step governance reuses the Phase 3 policy / capability / digest+approval /
  idempotency / audit path. Approvals bind to the **step** digest; the batch
  digest is evidence and replay detection only, never an authorization token.
  One request is never one authorization decision for mutations.
* Any mutation step forces `effective_parallelism == 1`.

## Recorded result

`docs/v2/evidence/phase4-batch-acceptance.json` — `gate: BATCH_ACCEPTED`,
`failures: []`, `max_observed_inflight: 2`.

V1 unchanged: contract `1.0.0`, schema `0.6.1`, 27 tools, HMAC policy
fail-closed. BATCH is not wired to MCP and `BATCH_FEATURE_ENABLED` remains
`False`.
