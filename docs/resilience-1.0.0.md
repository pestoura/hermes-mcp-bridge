# Hermes MCP Bridge 1.0.0 — controlled resilience

## Status

This document defines the retry-safety contract for the `1.0.0` release line.
It does not enable retry or circuit breaking in production. Both remain off by
default until the implementation, fault tests and isolated acceptance gates are
complete.

## Core rule

Automatic retries are based on the semantic operation, not only on the HTTP
method or status code.

A transport failure after a mutating request is ambiguous: the upstream may
have accepted the operation even when the bridge did not receive the response.
Replaying such a request can create duplicate sessions, runs or side effects.
Therefore ambiguous mutations are never automatically replayed.

## Operation classes

| Class | Automatic retry | Examples |
|---|---:|---|
| `safe_read` | permitted with bounded attempts | health, capabilities, session history, run status |
| `idempotent_write` | permitted only with a proven upstream idempotency contract | none approved initially |
| `ambiguous_mutation` | prohibited | create session, create run, stop run |
| `stream` | prohibited at request layer | run SSE event stream; recovery uses polling fallback |
| `unknown` | prohibited | any unclassified future route |

The canonical mapping is implemented in:

```text
src/hermes_mcp_bridge/resilience/operations.py
```

Unknown operations fail closed.

## Retryable read failures

The initial allow-list for safe reads is:

```text
408, 425, 429, 500, 502, 503, 504
```

Transport errors may also be retried for approved safe reads. Attempts remain
bounded by the configured maximum and must use the existing deterministic
backoff/jitter implementation.

The following are not retryable by this policy:

- authentication or authorization failures;
- validation failures;
- resource-not-found responses;
- any response from an ambiguous mutation;
- exhausted attempt budgets;
- SSE stream disconnects at the generic HTTP-request layer.

## SSE recovery

The run-event stream already converges to polling when the stream closes or
fails. It must not be wrapped in generic HTTP retry logic, because that can
create competing event readers and duplicate progress delivery.

## Production gates

Retry may only be enabled after all of the following are proved:

1. the HTTP client consumes the canonical operation policy;
2. only safe reads enter the retry executor;
3. create-session, create-run and stop-run are never replayed after ambiguous
   transport failures;
4. 429 and 5xx handling respects bounded backoff and `Retry-After` where valid;
5. metrics expose attempts and outcomes with low-cardinality labels only;
6. logs contain no prompts, outputs, tokens, cookies, paths or raw exceptions;
7. fault tests cover timeouts, disconnects, 429, each allowed 5xx and attempt
   exhaustion;
8. SSE-to-polling convergence produces no duplicate submission;
9. no run, lease or SQLite state is left stuck;
10. isolated and single-slot acceptance are both green.

## Rollout posture

For the first candidate containing this classification:

```text
BRIDGE_RETRY_ENABLED=false
BRIDGE_CIRCUIT_ENABLED=false
```

A later increment may wire the policy into the client while retaining these
safe defaults. Production activation is a separate operator decision and must
have an independent rollback gate.
