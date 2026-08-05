# Resilience and concurrency (Block 3)

> Contract version **0.9.0** (additive over 0.8.0). No schema change
> (`schema_version` stays `0.6.1`), no new MCP tool, no new runtime dependency.
> Production remains on its current version until an explicit rollout.

This document describes the deterministic concurrency, fault-tolerance and
recovery work delivered in 0.9.0 for `hermes-mcp-bridge`.

- [Scope and non-goals](#scope-and-non-goals)
- [SQLite concurrency model](#sqlite-concurrency-model)
- [Retry and backoff](#retry-and-backoff)
- [Circuit breaker](#circuit-breaker)
- [SSE to polling convergence](#sse-to-polling-convergence)
- [Recovery after crash or restart](#recovery-after-crash-or-restart)
- [Fault injection kit](#fault-injection-kit)
- [Load and soak harness](#load-and-soak-harness)
- [Observability](#observability)
- [Configuration](#configuration)
- [Compatibility](#compatibility)

## Scope and non-goals

In scope: determinism of the state layer under concurrency, bounded retry and
backoff, a per-upstream circuit breaker, idempotent SSE/polling convergence,
post-crash recovery, deterministic fault injection, a CI-safe load harness and
optional soak profiles.

Non-goals: new MCP tools, protocol/schema changes, deployment, distributed
locking across hosts, and any change to prompt or output handling.

## SQLite concurrency model

All state registries (`registry`, `approvals`, `locks`, `migrations`) share the
same rules:

- `journal_mode=WAL`, so readers never block the single writer. Note that
  `PRAGMA journal_mode=WAL` is itself **not** covered by `busy_timeout`: SQLite
  returns `SQLITE_BUSY` immediately when another connection holds the file
  during the switch. `_ensure_wal()` therefore retries the pragma in a bounded
  loop (50 x 10 ms) and re-raises on exhaustion. Without this, concurrent
  migrators fail intermittently with `database is locked` (reproduced ~4/12
  runs before the fix, 0/30 after);
- `busy_timeout` is set **on connection open, before any statement that can take
  a lock**. Setting it after `PRAGMA journal_mode=WAL` is a bug: the first WAL
  switch itself needs an exclusive lock and fails immediately with
  `database is locked` under concurrency;
- every read-modify-write sequence runs inside `BEGIN IMMEDIATE`, so the write
  lock is taken up-front and version checks cannot interleave;
- `executescript()` commits the open transaction, so the migrator re-takes
  `BEGIN IMMEDIATE` right after each DDL step to keep the ledger insert and the
  DDL under one serialised migrator;
- uniqueness is enforced by the schema (`INSERT OR IGNORE` / unique indexes),
  not by application-level check-then-write.

Properties verified by `tests/test_sqlite_concurrency.py` (multi-thread and
multi-process):

| Property | Test |
| --- | --- |
| No lost update on distinct keys | `test_concurrent_distinct_keys_have_no_lost_updates` |
| No torn row on status updates | `test_status_updates_never_interleave_into_a_torn_row` |
| Approval consumed exactly once | `test_approval_is_consumed_exactly_once_under_contention` |
| Single approval responder wins | `test_approval_respond_is_single_winner` |
| Exclusive lock has one owner | `test_write_exclusive_lock_has_single_owner_under_contention` |
| Acquire/release cycles do not deadlock | `test_acquire_release_cycles_do_not_deadlock` |
| Each migration applies once | `test_concurrent_migrations_apply_each_version_once` |
| No corruption across processes | `test_multi_process_writes_do_not_corrupt_the_database` |

## Retry and backoff

`resilience.retry.run_with_retry` retries **only** transient SQLite contention
(`database is locked` / `database is busy`). Logic errors, integrity errors and
domain errors are never retried. The attempt count is bounded and exhaustion
raises `RetryExhaustedError` instead of looping.

`resilience.backoff.BackoffPolicy` produces a deterministic exponential
schedule. Jitter is seedable, so a given seed always reproduces the same
sequence, and every delay is clamped by `max_seconds` and a global cap.
`parse_retry_after` accepts both the delta-seconds and HTTP-date forms, rejects
malformed values, and clamps the result.

## Circuit breaker

`resilience.circuit.CircuitBreaker` implements closed → open → half-open with
an injectable clock, so tests do not sleep:

- `failure_threshold` consecutive failures in **closed** open the circuit;
- while **open**, calls are rejected with `CircuitOpenError` and counted;
- after `recovery_seconds`, the breaker moves to **half-open** and admits at
  most `half_open_max_calls` probes concurrently;
- `success_threshold` successful probes close it; a single failure in half-open
  reopens it immediately.

The breaker snapshot exposes counters and state only — no identifiers.

## SSE to polling convergence

`resilience.events.RunStateTracker` makes run-state application idempotent:

- each transition is applied at most once;
- a duplicated terminal event does not double-count;
- an out-of-order or lower-sequence event cannot regress a terminal state;
- two *conflicting* terminal states raise `TerminalStateError` rather than
  silently picking one;
- SSE and polling observing the same run converge on a single completion.

On a truncated, invalid or reset stream the bridge falls back to polling and
still returns the real result. `Retry-After` from the upstream is honoured and
bounded. Cancellation never marks a run successful, releases tracker resources
and can request an upstream stop.

## Recovery after crash or restart

`resilience.recovery.recover_state` runs against a real SQLite file and:

- reports only non-terminal runs, so terminal work is not reprocessed;
- keeps `execution_id` and run mappings readable after a restart;
- **never resubmits** — recovery is read/repair only, so zero duplicate
  submissions;
- reaps expired locks only, leaving live locks untouched;
- clears leftover in-flight gauges;
- is a no-op on a fresh, empty database;
- leaves no partial mapping when a crash happens mid-persistence.

The report contains aggregated counts and truncated fingerprints only.

## Fault injection kit

`tests/faultkit/` is **test-only** and is never imported by the runtime package
(asserted by `test_faultkit_is_not_imported_by_runtime_package`). It is
deterministic and seedable:

- `http.py` — timeouts, connection resets, `429/500/502/503`, scripted
  responses and rate-based profiles;
- `sse.py` — truncated bodies, malformed frames, duplicated terminal frames and
  out-of-order frames;
- `sqlite.py` — exactly-N or rate-based `OperationalError`/busy injection,
  per-handle flaky connections, and a simulated disk-full connection where
  writes fail but reads succeed.

Given the same seed, every profile reproduces the same failure sequence.

## Load and soak harness

`scripts/load_harness.py` drives real SQLite contention against the real
registries. It never touches the Hermes API, never opens a socket and writes
only inside a temporary directory (or `--db`).

| Profile | Duration | Intended use |
| --- | --- | --- |
| `ci` | 5 s | runs in CI |
| `soak-30m` | 30 min | manual, outside CI |
| `soak-60m` | 60 min | manual, outside CI |
| `soak-2h` | 2 h | manual, outside CI |

FAIL (non-zero exit) if there is any unexpected error, if
`PRAGMA integrity_check` is not `ok`, if run mappings are duplicated, if any
approval was double-consumed, or if the error ratio exceeds
`--max-error-ratio`. The JSON report is sanitized: aggregated counters,
durations and truncated fingerprints only.

The harness forces `LOG_LEVEL=WARNING` by default, because expected contention
is the workload and per-event logs would be unbounded on soak profiles.

> The soak profiles are **available**, not executed as part of this delivery.
> Only the `ci` profile was run here.

## Observability

New bounded metrics (all counters unless stated):

| Metric | Labels | Meaning |
| --- | --- | --- |
| `bridge_sqlite_retries_total` | `kind` | Bounded retries after transient contention |
| `bridge_circuit_transitions_total` | `upstream`, `state` | Breaker state transitions |
| `bridge_circuit_rejections_total` | `upstream` | Calls rejected while open |
| `bridge_duplicate_events_total` | `source` | Duplicate events ignored |
| `bridge_out_of_order_events_total` | `source` | Regressing events ignored |
| `bridge_recovery_runs_total` | `outcome` | Runs recovered after restart |
| `bridge_backoff_sleep_seconds` (histogram) | `source` | Bounded backoff sleeps |

Cardinality is provably finite: `state`, `source` and `upstream` have
allow-listed value domains (`BOUNDED_LABEL_VALUES`) and any value outside the
domain is folded into `other` instead of creating a new series. Identifier-like
labels remain rejected by the existing `FORBIDDEN_LABELS` policy. No prompt,
output, secret, session id, execution id or path is ever emitted.

## Configuration

All resilience features are **off by default**, so upgrading cannot change the
number of requests an operator observes.

| Variable | Default | Meaning |
| --- | --- | --- |
| `BRIDGE_RETRY_ENABLED` | `false` | Enable bounded upstream retry |
| `BRIDGE_RETRY_MAX_ATTEMPTS` | `3` | 1–10 |
| `BRIDGE_RETRY_BASE_SECONDS` | `0.5` | > 0, ≤ 60 |
| `BRIDGE_RETRY_MAX_SECONDS` | `10.0` | > 0, ≤ 300 |
| `BRIDGE_RETRY_JITTER_RATIO` | `0.1` | 0–1 |
| `BRIDGE_CIRCUIT_ENABLED` | `false` | Enable the circuit breaker |
| `BRIDGE_CIRCUIT_FAILURE_THRESHOLD` | `5` | 1–1000 |
| `BRIDGE_CIRCUIT_RECOVERY_SECONDS` | `30.0` | > 0, ≤ 3600 |
| `BRIDGE_CIRCUIT_HALF_OPEN_MAX_CALLS` | `1` | 1–100 |
| `BRIDGE_CIRCUIT_SUCCESS_THRESHOLD` | `1` | 1–100 |

Bounds are enforced by the settings model, so a mistyped value fails fast
instead of producing an unbounded retry storm.

## Compatibility

- No tool was added, removed or renamed; the 27 tools of 0.8.0 are unchanged.
- `schema_version` stays `0.6.1`; no migration is added by this release.
- `bridge_version` and `manifest_version` move to `0.9.0`;
  `hermes_readiness.version_added` stays `0.8.0`, since the tool itself did not
  change.
- All new configuration defaults to off, so behaviour is identical to 0.8.0
  until an operator opts in.

See also [`compatibility.md`](compatibility.md) and
[`observability.md`](observability.md).
