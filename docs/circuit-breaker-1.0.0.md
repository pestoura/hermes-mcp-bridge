# Hermes MCP Bridge 1.0.0 — selective upstream circuit breaker

## Objective

Add bounded fail-fast protection for repeated upstream availability failures
without hiding or duplicating a mutation. The breaker is disabled by default
and may be enabled only as a separate operational gate after retry has been
validated independently.

The circuit policy is fail-closed: a request is protected only when its HTTP
method and normalized path match the same finite read-only allow-list used by
selective retry.

## Protection matrix

| Operation | HTTP request | Circuit | Reason |
| --- | --- | --- | --- |
| Hermes liveness/readiness | `GET /health`, `GET /health/detailed` | Protected | Read-only and idempotent |
| Run status | `GET /v1/runs/{execution_id}` | Protected | Read-only and idempotent |
| Session message history | `GET /api/sessions/{session_id}/messages` | Protected | Read-only and idempotent |
| Session creation | `POST /api/sessions` | Excluded | A transport error can hide a created session |
| Run submission | `POST /v1/runs` | Excluded | A transport error can hide an accepted run |
| Run stop | `POST /v1/runs/{execution_id}/stop` | Excluded | Mutation without proven upstream idempotency |
| SSE event stream | `GET /v1/runs/{execution_id}/events` | Excluded | SSE-to-polling convergence is the recovery path |
| Unknown endpoint or method | Any | Excluded | Unknown means unsafe |

No mutation is rejected because of circuit state. Mutation failures continue to
follow their existing single-attempt and recovery semantics.

## Failure classification

A protected logical operation counts as a circuit failure only after its safe
retry policy, when enabled, has been exhausted.

Failures that count:

- final connection/request transport errors;
- final request timeouts;
- HTTP `429`;
- HTTP `500`, `502`, `503` and `504`.

Responses that prove reachability and therefore do not poison the circuit:

- permanent `4xx` responses other than `429`;
- HTTP `501`;
- successful responses.

This classification prevents invalid credentials, missing runs or unsupported
operations from opening an availability circuit.

## State machine

Each safe endpoint class (`health`, `runs`, `sessions`) has one process-local
breaker:

1. **closed** — calls are admitted; consecutive logical failures are counted;
2. **open** — calls are rejected locally without network access;
3. **half-open** — after the recovery interval, a bounded number of probes is
   admitted;
4. enough successful probes close the breaker; one failed probe reopens it.

The breaker is process-local. A bridge restart resets breaker state; it never
persists endpoint failures to SQLite and never affects run or lease records.

## Configuration

```text
BRIDGE_CIRCUIT_ENABLED=false
BRIDGE_CIRCUIT_FAILURE_THRESHOLD=5
BRIDGE_CIRCUIT_RECOVERY_SECONDS=30.0
BRIDGE_CIRCUIT_HALF_OPEN_MAX_CALLS=1
BRIDGE_CIRCUIT_SUCCESS_THRESHOLD=1
```

Bounds are enforced before server startup:

- failure threshold: `1..1000`;
- recovery interval: `>0` and `<=3600s`;
- half-open calls: `1..100`;
- success threshold: `1..100`.

## Interaction with retry

The breaker surrounds the retry loop once per logical operation:

```text
circuit acquire
  -> bounded safe retry loop
  -> one success or one final failure recorded in the circuit
```

Individual retry attempts never increment the breaker failure counter. This
prevents one logical request with three attempts from opening a threshold-three
circuit immediately.

Retry and circuit breaker remain independent configuration gates. Production
activation must validate retry first, return to a stable observation window,
and only then enable the circuit breaker.

## Observability and data minimization

Health/readiness expose only:

- enabled/disabled;
- the finite protected endpoint classes;
- configured numeric thresholds;
- sanitized breaker name, state and aggregate counters;
- `mutations_protected=false`;
- `sse_protected=false`.

Metrics use finite endpoint and state labels. No raw URL, path, run ID, session
ID, prompt, output, token, cookie, secret or filesystem path is emitted.

An open-circuit client error is intentionally generic:

```text
Hermes API temporarily unavailable (circuit open)
```

## Isolated validation

Before production activation:

1. run the complete Python 3.11 and 3.12 suite;
2. prove the disabled configuration preserves existing behavior;
3. inject repeated transient failures into every protected GET class;
4. prove the configured threshold opens exactly one logical breaker;
5. prove an open breaker rejects without network access;
6. prove permanent `4xx` and `501` responses do not open it;
7. prove POST mutations and SSE remain outside the breaker;
8. prove retry attempts count as one logical failure;
9. advance an injected clock and prove bounded half-open recovery;
10. prove health, logs and metrics contain no sensitive or high-cardinality data.

## Production activation order

1. keep `BRIDGE_CIRCUIT_ENABLED=false` in the initial `1.0.0` deployment;
2. complete the single-slot Hermes/RITMO acceptance;
3. validate selective retry as a separate gate;
4. enable the breaker on one candidate instance only;
5. exercise read-only health, status and session-history calls;
6. observe transitions, rejections, latency and upstream error rate;
7. prove no run, lease or state transition is affected;
8. expand only after an explicit acceptance decision.

## Rollback

Circuit rollback is configuration-only:

```text
BRIDGE_CIRCUIT_ENABLED=false
```

Restart through the controlled deployment path and verify:

- health and readiness;
- exact 27-tool contract;
- schema `0.6.1`;
- breaker posture disabled;
- no further circuit transitions or rejections.

A circuit-gate failure does not require rolling back the full `1.0.0` codebase
unless disabled behavior differs from the previous baseline.

## Acceptance decision

The isolated gate may be declared only after all directed and fault-injection
evidence is green:

```text
HERMES_BRIDGE_1_0_0_CIRCUIT_GATE_PASS
```

This decision does not replace the mandatory Hermes/RITMO single-slot
acceptance and does not authorize production deployment of `1.0.0`.
