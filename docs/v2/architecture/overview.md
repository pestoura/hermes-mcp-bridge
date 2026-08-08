# V2 Architecture Overview

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

## Architectural intent

V2 evolves the current `hermes-mcp-bridge`; it does not create a separate product. The preferred architectural name is **Hermes Execution Gateway**, alternatively **Secure Execution Control Plane**.

```text
ChatGPT / MCP client
        |
        | typed intent / plan
        v
Hermes MCP Bridge v2
  +-----------------------------+
  | capability projection       |
  | schema validation           |
  | policy / approvals          |
  | budgets / quotas / locks    |
  | scheduler / runbooks        |
  | credential broker           |
  | result shaping / evidence   |
  +-----------------------------+
        |
        +--> typed tools / APIs / CLIs / internal MCPs
        |
        +--> Hermes Agent / LLM only when reasoning is required
```

## Control rule

ChatGPT decides/intends. Hermes controls whether execution is permitted. A typed tool executes. The Hermes LLM participates only when reasoning is required.

V2 must not add a Router LLM between ChatGPT and Hermes. Routing is deterministic and can be expressed directly by the selected MCP tool: `github.get_pr` -> DIRECT, `hermes.batch_execute` -> BATCH, `hermes.execute_dag` -> DAG, `hermes.runbook_execute` -> RUNBOOK, `hermes_prompt` -> AGENTIC.

## Main components

1. **Tool Registry** — canonical typed capability metadata and backend provenance.
2. **Capability Projection** — policy/allowlist filtered external tool surface.
3. **Credential Broker** — capability-scoped credential resolution without secret disclosure.
4. **Policy/Governance** — per-node policy, scopes, approvals, locks, quotas and immutable digests.
5. **Execution Scheduler** — bounded parallel BATCH/DAG execution, rate limits and backpressure.
6. **Runbook Registry** — versioned, validated, testable deterministic workflows.
7. **Result/Evidence Layer** — shaping, artifact references, signed manifests and provenance.
8. **Agentic Escalation Layer** — bounded and explicit reasoning fallback.
9. **Observability** — metrics, W3C trace context, token economics and execution evidence.

## Invariants

- unknown tool/action: DENY;
- missing policy: DENY;
- missing credential: FAIL;
- schema mismatch: FAIL;
- ambiguous typed binding: FAIL;
- unrestricted generic shell is not projected as the normal external execution mechanism;
- secrets are never serialized back to the client;
- each BATCH/DAG node is governed independently;
- mutations are idempotent/replay-protected where possible;
- partial failure is explicit;
- no claim of ACID transactions across heterogeneous external systems.
