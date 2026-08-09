# BATCH Aggregation and Evidence

> **V2 · PHASE 4 · DESIGN ONLY · NOT_IMPLEMENTED · DO_NOT_MERGE UNTIL DIRECT_MUTATION_ACCEPTED**

## Aggregate status algebra

Evaluated in this order; the first matching rule wins.

| # | Condition | `aggregate_status` |
|---|---|---|
| 1 | Envelope/budget validation failed | `DENIED` |
| 2 | Batch wall clock exceeded | `TIMED_OUT` |
| 3 | Cancelled (fail-fast or caller disconnect) **and** no step `SUCCESS` | `CANCELLED` |
| 4 | All steps `SUCCESS` | `SUCCESS` |
| 5 | At least one `SUCCESS` and at least one non-`SUCCESS` | `PARTIAL` |
| 6 | No `SUCCESS` and every non-success is `DENIED` | `DENIED` |
| 7 | Otherwise | `FAILED` |

`PARTIAL` is a first-class, expected outcome. Callers must handle it; it is
never collapsed into `SUCCESS` or `FAILED`.

## Counts invariant

`counts` maps every status present to its total and satisfies
`sum(counts.values()) == len(steps)`. Statuses with zero occurrences may be
omitted. This is a machine-checkable acceptance invariant (S-05).

## Evidence record

One JSON evidence record per batch, written through the Phase 3 evidence path:

```json
{
  "kind": "v2.batch.execution",
  "batch_id": "<caller id>",
  "batch_digest": "<sha256 over batch_id + ordered step digests>",
  "schema_version": "<batch schema constant>",
  "failure_policy": "fail_fast|continue_on_error",
  "requested_parallelism": 4,
  "effective_parallelism": 2,
  "max_items_ceiling": 10,
  "dry_run": false,
  "started_at": "RFC3339",
  "finished_at": "RFC3339",
  "aggregate_status": "PARTIAL",
  "counts": {"SUCCESS": 2, "FAILED": 1, "NOT_STARTED": 1},
  "external_calls_total": 3,
  "max_observed_inflight": 2,
  "steps": [
    {
      "step_id": "s1",
      "tool": "github.search",
      "status": "SUCCESS",
      "started_at": "RFC3339",
      "finished_at": "RFC3339",
      "idempotency_outcome": "EXECUTED",
      "audit_ref": "<id>",
      "error_code": null
    }
  ]
}
```

Rules:

- `max_observed_inflight` is recorded so that non-serial execution is provable
  from evidence alone, not only from timing.
- Only typed error **codes** appear; messages are redacted through the Phase 3
  path. No provider payloads, tokens, headers, URLs with secrets, or arguments
  that may carry sensitive values.
- Step arguments are **not** stored in evidence; the step digest covers them.
- Evidence is written for `DENIED` batches too, with `steps: []`.

## Metrics

Counters/histograms exposed through the existing observability surface:
`batch_requests_total{aggregate_status}`, `batch_steps_total{status}`,
`batch_effective_parallelism`, `batch_duration_seconds`,
`batch_admission_rejected_total{reason}`. No new exporter, no new endpoint.
