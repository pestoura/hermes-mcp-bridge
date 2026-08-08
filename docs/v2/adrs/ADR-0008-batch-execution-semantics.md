# ADR-0008 — BATCH Execution Semantics

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Independent operations currently require multiple bridge/agent cycles or serial agent orchestration.

## Decision
Allow one request to contain N independent typed nodes executed through bounded parallel scheduling. Each node retains independent policy, scope, credential, risk, quota, audit and shaping. Aggregate state explicitly represents partial success.

## Consequences
Lower round trips/latency but introduces scheduler/backpressure/fairness complexity.

## Alternatives
One MCP call per operation; agent-driven sequential orchestration.

## Security implications
Budgets and per-node governance are mandatory to prevent fan-out DoS or approval overreach.

## Operational implications
Need provider-aware limits, adaptive concurrency, circuit breakers and queue metrics.

## Open questions
Default concurrency, retry and fairness settings.
