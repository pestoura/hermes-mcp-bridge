# Hermes MCP Bridge 1.0.0 — selective upstream retry

## Objective

Add bounded resilience for transient failures **without creating a second
mutation**. Retry is disabled by default and may be enabled only as a separate
operational gate after isolated validation.

The policy is fail-closed: an operation is retryable only when both its HTTP
method and its normalized path exactly match the allow-list in
`resilience/http_retry.py`.

## Retry safety matrix

| Operation | HTTP request | Retry | Reason |
| --- | --- | --- | --- |
| Hermes liveness/readiness | `GET /health`, `GET /health/detailed` | Allowed | Read-only and idempotent |
| Run status | `GET /v1/runs/{execution_id}` | Allowed | Read-only and idempotent |
| Session message history | `GET /api/sessions/{session_id}/messages` | Allowed | Read-only and idempotent |
| Session creation | `POST /api/sessions` | Forbidden | A transport error can hide a created session |
| Run submission | `POST /v1/runs` | Forbidden | A transport error can hide an accepted run |
| Run stop | `POST /v1/runs/{execution_id}/stop` | Forbidden | Mutation without a proven upstream idempotency key |
| SSE event stream | `GET /v1/runs/{execution_id}/events` | Forbidden | Existing SSE-to-polling convergence is the recovery path |
| Unknown endpoint | Any | Forbidden | Unknown means unsafe |

The existing duplicate-session-title loop is not an HTTP transport retry. It
runs only after Hermes explicitly returns the known `title already in use`
validation response and creates a new unique title for the next request.

## Transient conditions

For allow-listed GET operations only, the client may retry:

- connection/request transport errors;
- request timeouts;
- HTTP `429`;
- HTTP `500`, `502`, `503` and `504`.

Other `4xx` responses, including `400`, `401`, `403`, `404`, `409` and `422`,
are returned immediately to the normal decoder and are not retried. HTTP `501`
is also excluded because it normally represents an unsupported operation, not a
transient failure.

A valid `Retry-After` header in delta-seconds or HTTP-date form is honoured but
remains bounded by the global 300-second safety cap. Malformed, negative or
excessive values are ignored in favour of the configured exponential backoff.

## Attempt semantics

`BRIDGE_RETRY_MAX_ATTEMPTS` is the **total** number of requests, including the
initial request. With the default value `3`, at most two retry sleeps occur.
There is no unbounded loop.

```text
BRIDGE_RETRY_ENABLED=false
BRIDGE_RETRY_MAX_ATTEMPTS=3
BRIDGE_RETRY_BASE_SECONDS=0.5
BRIDGE_RETRY_MAX_SECONDS=10.0
BRIDGE_RETRY_JITTER_RATIO=0.1
```

Settings bounds are enforced by Pydantic before the server starts:

- attempts: `1..10`;
- base delay: `>0` and `<=60s`;
- maximum delay: `>0` and `<=300s`;
- jitter ratio: `0..1`.

## Observability

A scheduled retry emits only bounded, non-sensitive fields:

```text
bridge_upstream_retries_total{endpoint_class,reason}
bridge_upstream_retry_delay_seconds{endpoint_class,reason}
```

Allowed endpoint classes are `health`, `runs` and `sessions`. Reasons are folded
into the existing finite metric domain (`timeout`, `connect_error`,
`http_error`, `other`). No URL, path, run ID, session ID, prompt, output or
credential becomes a metric label.

`hermes_health.bridge.observability.retry` reports only:

- enabled/disabled;
- effective maximum attempts;
- safe endpoint classes;
- `mutations_retryable=false`;
- `sse_retryable=false`.

## Isolated validation

Before any production activation:

1. run the full Python 3.11/3.12 suite;
2. inject timeout, connection reset, `429`, `500`, `502`, `503` and `504` into
   each allow-listed GET path;
3. prove a successful later response is returned once;
4. prove `404` and other permanent responses perform exactly one request;
5. inject timeout and `503` into all three mutation classes and prove exactly
   one POST occurs;
6. prove the attempt count and delay are bounded;
7. prove `Retry-After` cannot exceed the global cap;
8. prove retry metrics contain no identifiers or sensitive content;
9. run a real read-only candidate against an isolated Hermes API;
10. keep circuit breaker disabled during this gate.

## Production activation order

Retry must not be enabled at the same time as a new circuit-breaker policy.
Promote one mechanism at a time:

1. keep `BRIDGE_CIRCUIT_ENABLED=false`;
2. enable retry for one candidate instance;
3. validate health/readiness and the exact 27-tool contract;
4. execute read-only health, status and session-history probes;
5. inject or observe a controlled transient failure;
6. confirm bounded retries and no duplicate mutation;
7. maintain an observation window;
8. only then consider a separate circuit-breaker gate.

## Rollback

Retry rollback is configuration-only:

```text
BRIDGE_RETRY_ENABLED=false
```

Restart the bridge through the controlled deployment path, then verify:

- health and readiness;
- 27 tools;
- schema `0.6.1`;
- retry posture reports disabled;
- no additional retry metrics increase.

A retry-gate failure does not require rolling back the entire `1.0.0` codebase
unless the disabled configuration also changes request behaviour.

## Acceptance decision

The isolated retry gate may be declared only after the safety matrix and fault
injection evidence are complete:

```text
HERMES_BRIDGE_1_0_0_RETRY_GATE_PASS
```

This decision does not replace the required Hermes/RITMO single-slot
acceptance and does not authorize production deployment of `1.0.0`.
