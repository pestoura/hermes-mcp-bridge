# Observability (Block 2)

Structured logging, exportable metrics, health/readiness and optional tracing
for `hermes-mcp-bridge`. Everything here is **off or loopback-only by default**
and passes through central, fail-closed redaction.

- [Configuration](#configuration)
- [Event catalogue](#event-catalogue)
- [Metric catalogue](#metric-catalogue)
- [Cardinality policy](#cardinality-policy)
- [Privacy and redaction](#privacy-and-redaction)
- [Health and readiness](#health-and-readiness)
- [Tracing](#tracing)
- [Dashboards and minimum alerts](#dashboards-and-minimum-alerts)
- [Runbook](#runbook)

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BRIDGE_LOG_FORMAT` | `json` | `json` (safe default, used in production/containers) or `text` for local debugging. Any other value falls back to `json`. |
| `BRIDGE_LOG_LEVEL` | `INFO` | Level of the `hermes_mcp_bridge` logger. Falls back to `LOG_LEVEL`. |
| `BRIDGE_LOG_REDACT_FIELDS` | *(empty)* | Comma-separated extra field names to always redact. |
| `BRIDGE_METRICS_ENABLED` | *(unset = off)* | `1` enables the Prometheus exporter. |
| `BRIDGE_METRICS_HOST` | `127.0.0.1` | Exporter bind address. Non-loopback requires explicit opt-in. |
| `BRIDGE_METRICS_PORT` | `9464` | Exporter port. |
| `BRIDGE_METRICS_ALLOW_REMOTE` | *(unset = off)* | Required to bind a non-loopback address. |
| `BRIDGE_METRICS_TOKEN` | *(unset)* | Bearer token required on `/metrics`. Mandatory for remote binding. |
| `BRIDGE_TRACING_ENABLED` | *(unset = off)* | Enables span creation (still no-op unless OTel is installed). |
| `BRIDGE_TRACING_EXPORT` | *(unset = off)* | Enables OpenTelemetry export. Off by default. |

No secrets are ever placed in these variables except `BRIDGE_METRICS_TOKEN`,
which is only read, never logged and never returned by health/readiness.

There is **no new runtime dependency**: metrics are rendered by a small
in-tree Prometheus text encoder. OpenTelemetry is imported lazily and is
entirely optional — the bridge boots and runs without it.

## Event catalogue

All events are single-line JSON with UTC millisecond timestamps and sorted keys.

| Event | Level | Key fields |
| --- | --- | --- |
| `bridge.startup` | INFO | `bridge_version`, `instrumented_tools`, `outcome` |
| `bridge.tool.call` | INFO | `tool`, `outcome` (`success`/`error`/`cancelled`), `duration_ms` |
| `bridge.upstream.request` | INFO | `endpoint_class`, `status_class`, `outcome`, `duration_ms` |
| `bridge.sse.fallback` | INFO | `reason`, `outcome=fallback` |
| `log.format_failed` | ERROR | emitted by the formatter itself when serialization fails |

Every record additionally carries whichever correlation fields are bound:
`correlation_id`, `trace_id`, `span_id`, `execution_id`, `run_id`,
`session_id`, `tool_name`.

Sanitized example:

```json
{"correlation_id":"6f1b1c1e9a0b4c1f8a2d3e4f5a6b7c8d","duration_ms":184.221,"event":"bridge.tool.call","level":"INFO","logger":"hermes_mcp_bridge","outcome":"success","tool":"hermes_prompt","ts":"2026-08-04T14:22:03.118Z"}
{"duration_ms":42.004,"endpoint_class":"runs","event":"bridge.upstream.request","level":"INFO","logger":"hermes_mcp_bridge","outcome":"success","status_class":"2xx","ts":"2026-08-04T14:22:03.160Z"}
{"event":"bridge.sse.fallback","level":"INFO","logger":"hermes_mcp_bridge","outcome":"fallback","reason":"stream_ended","ts":"2026-08-04T14:22:48.902Z"}
```

## Metric catalogue

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `bridge_tool_calls_total` | counter | `tool`, `outcome` | MCP tool invocations |
| `bridge_tool_duration_seconds` | histogram | `tool` | Tool latency |
| `bridge_tool_inflight` | gauge | `tool` | Concurrent tool invocations |
| `bridge_upstream_requests_total` | counter | `endpoint_class`, `status_class` | Hermes API calls |
| `bridge_upstream_duration_seconds` | histogram | `endpoint_class` | Hermes API latency |
| `bridge_sse_connections_total` | counter | `outcome` | SSE connection attempts |
| `bridge_sse_fallbacks_total` | counter | `reason` | SSE → polling fallbacks |
| `bridge_polling_iterations_total` | counter | — | Polling loop iterations |
| `bridge_active_runs` | gauge | — | Runs observed as active |
| `bridge_approvals_total` | counter | `decision` | Approval decisions |
| `bridge_sqlite_errors_total` | counter | `kind` | SQLite errors |
| `bridge_sqlite_lock_contention_total` | counter | — | Lock/busy events |
| `bridge_migrations_version` | gauge | — | Applied schema version |
| `bridge_info` | gauge | `version` | Always `1`, carries build version |
| `bridge_observability_errors_total` | counter | `kind` | Internal telemetry failures |

`endpoint_class` values: `runs`, `run_events`, `run_stop`, `sessions`,
`health`, `other`. `status_class` values: `1xx`…`5xx`, `error`.

## Cardinality policy

Enforced in code, not by convention:

- Label names must be in the allow-list: `tool`, `outcome`, `endpoint_class`,
  `status_class`, `decision`, `reason`, `mode`, `version`, `kind`.
- Forbidden label names raise `CardinalityError`: `run_id`, `session_id`,
  `execution_id`, `client_request_id`, `approval_id`, `correlation_id`,
  `trace_id`, `user`, `prompt`, `output`, `path`, `url`, `token`, `api_key`.
- Label values are truncated to 64 characters.
- Each metric is capped at 200 series (`MAX_SERIES_PER_METRIC`); beyond that new
  series are dropped rather than growing memory.

A regression test asserts that the label names actually present in the registry
after exercising the bridge are a subset of the allow-list.

## Privacy and redaction

Redaction is central and fail-closed:

- Field names matching secrets (`authorization`, `token`, `api_key`, `secret`,
  `password`, `cookie`, `private_key`, `prompt`, `output`, `content`,
  `messages`, `traceback`, …) are replaced with `[REDACTED]`.
- Free text is scrubbed for `Bearer …`, `Basic …`, `Authorization:` headers,
  `key=value` secret assignments, JWTs, PEM private keys and known key prefixes.
- Filesystem paths become `[PATH]` in free text and `[PATH:<ext>]` in path fields.
- `approval_id` is fingerprinted (`appr…<sha256 prefix>`), never emitted in full.
- Exceptions become `{"type": ..., "message": <redacted>}`. Stack traces are
  never serialized.
- Arbitrary objects are **never** `repr()`/`str()`-ed; they collapse to
  `[TypeName]`.
- Limits: depth 6, 50 items/keys, 512 chars per string, 8192 chars per record.

Telemetry never changes behaviour: a formatter, sink or registry failure is
swallowed and the tool call proceeds normally.

## Health and readiness

`hermes_health` gains an `observability` block (logging mode, metrics exporter
state and bind scope, registry status, tracing implementation). No paths,
tokens or keys are present.

`hermes_readiness` is a new read-only tool reporting each component separately:

| Component | Ready when |
| --- | --- |
| `upstream` | Hermes `/health` answers |
| `state_db` | run registry reports `up` |
| `approval_registry` | approval registry reports `up` |
| `metrics_registry` | in-memory registry is usable |
| `logging` | handler installed |
| `tracing` | always ready (no-op is a valid implementation) |
| `config` | settings loaded, API key present (boolean only) |

Overall status is `ready`, `degraded` (upstream/tracing only) or `not_ready`.
Readiness performs no `PRAGMA integrity_check` and no full table scans.

## Tracing

- No-op by default; spans always exist so call sites need no conditionals.
- W3C `traceparent` is parsed and validated (version `ff`, all-zero trace/span
  ids and non-lowercase hex are rejected); invalid input simply starts a new trace.
- Trace/span ids are bound into the correlation context, so logs correlate even
  without an exporter.
- OpenTelemetry is used only when installed **and** both `BRIDGE_TRACING_ENABLED`
  and `BRIDGE_TRACING_EXPORT` are on. Failures are fail-open — never for auth
  or policy.

## Dashboards and minimum alerts

Minimum dashboard panels:

1. Tool call rate and error ratio by `tool`.
2. p50/p95/p99 `bridge_tool_duration_seconds`.
3. `bridge_tool_inflight` over time.
4. Upstream request rate by `status_class` and upstream latency percentiles.
5. SSE fallbacks vs. polling iterations.
6. `bridge_active_runs` and `bridge_approvals_total` by `decision`.
7. `bridge_sqlite_errors_total` and `bridge_sqlite_lock_contention_total`.

Minimum alerts:

| Alert | Expression sketch | Severity |
| --- | --- | --- |
| Tool error ratio high | `rate(bridge_tool_calls_total{outcome="error"}[5m]) / rate(bridge_tool_calls_total[5m]) > 0.1` for 10m | page |
| Upstream 5xx | `rate(bridge_upstream_requests_total{status_class="5xx"}[5m]) > 0` for 10m | page |
| Tool latency p95 | `histogram_quantile(0.95, rate(bridge_tool_duration_seconds_bucket[5m])) > 60` | ticket |
| SSE degradation | `rate(bridge_sse_fallbacks_total[15m]) > 0.2` | ticket |
| SQLite contention | `rate(bridge_sqlite_lock_contention_total[5m]) > 0` | ticket |
| Schema drift | `changes(bridge_migrations_version[1h]) > 0` unexpectedly | ticket |
| Telemetry broken | `rate(bridge_observability_errors_total[15m]) > 0` | ticket |

## Runbook

### High latency

1. Compare `bridge_tool_duration_seconds` p95 with
   `bridge_upstream_duration_seconds` p95. If both rise, the cause is upstream.
2. Check `bridge_tool_inflight`: sustained growth means queuing/concurrency.
3. Grep logs for `bridge.tool.call` with high `duration_ms` and correlate via
   `correlation_id` to the matching `bridge.upstream.request`.

### SSE fallback

1. `bridge_sse_fallbacks_total` by `reason` identifies the trigger
   (`stream_ended`, HTTP rejection, timeouts).
2. `bridge_sse_connections_total{outcome="rejected"}` indicates upstream refusing
   the event stream (auth, path, proxy buffering).
3. Confirm `bridge_polling_iterations_total` rises accordingly — the bridge is
   degraded but functional.

### Upstream 5xx

1. `bridge_upstream_requests_total{status_class="5xx"}` grouped by
   `endpoint_class` narrows the failing surface (`runs`, `sessions`, `health`).
2. Run `hermes_readiness`; `upstream: not_ready` confirms full unavailability
   versus partial errors.
3. Escalate to the Hermes API side; the bridge retains state and does not
   discard runs.

### SQLite contention

1. `bridge_sqlite_lock_contention_total` rising with
   `bridge_sqlite_errors_total{kind="database is locked"}` indicates WAL contention.
2. Confirm only one bridge process writes the state DB.
3. Readiness stays cheap by design — do not add integrity checks to diagnose;
   run them offline against a copy.

### Pending approvals

1. `bridge_approvals_total` by `decision` shows the decision mix; a flat
   `approved`/`rejected` rate with active runs means approvals are stuck waiting.
2. Use `hermes_approval_status` with the specific approval id (never logged in
   full — only a fingerprint appears in logs).
3. Check expiry: expired approvals fail closed and must be recreated.
