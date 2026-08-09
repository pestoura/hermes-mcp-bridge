# ADR-0020 — Defer runtime-maintenance and concurrency hardening to V2.1

> **V2.1 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1 OR ON V2 IN PROGRESS**

**Status:** Proposed
**Date:** 2026-08-09

## Context

Two operational limits were observed while V2 delivery is in flight:

1. Any Hermes runtime maintenance (restart, upgrade, config change) is disruptive and partly invisible: readiness does not separate `alive` / `ready` / `accepting_new_work`, a maintenance action driven through Hermes can block its own drain, and there is no checkpoint/resume for active runs. Empirically recorded in issue #34.
2. Concurrency is a single hard cap. `gateway.api_server.max_concurrent_runs` defaults to **10** (`gateway/platforms/api_server.py`, `_resolve_max_concurrent_runs` / `_concurrency_limited_response`) and produces `429 Too many concurrent runs (max N)`. The bridge ingress (`http_runner.py`, uvicorn without `limit_concurrency`) is unbounded, so acceptance capacity and execution capacity are conflated. The real workload is 5 conversations x 5–6 agents = 25–30 concurrent calls plus margin.

Fixing either inside the current V2 lane would require touching runtime behaviour and restarting the runtime while the Phase 2 `DIRECT_READ_ACCEPTED` campaign is running.

## Decision

- Keep V2 delivery running without interruption. No restart, quiesce, rebase or code change is performed for these two items during V2.
- Defer both improvements to **V2.1**, tracked as issues #79 (zero-disruption maintenance / upgrade isolation) and #80 (high-concurrency admission control and scheduler) under milestone **V2.1**.
- V2.1 must be additive: no breaking change to the V2 tool contract, defaults preserving current behaviour.
- Concurrency is to be modelled in layers (ingress/in-flight admission vs runtime-heavy execution, plus per conversation/principal/project/resource limits), never as a single larger hard cap. Mutation concurrency on the same resource is not increased.

## Consequences

- V2 delivery is not blocked and cross-project interference is removed in the following release instead of mid-campaign.
- The `429` ceiling and disruptive restarts persist until V2.1 ships; operators must plan maintenance windows in the meantime.
- V2.1 inherits scheduler/backpressure/fairness complexity already anticipated in ADR-0008 and open decision OD-006.

## Alternatives

- Raise `max_concurrent_runs` immediately: rejected — moves failure from `429` to timeouts and memory exhaustion, and requires a runtime restart during V2.
- Implement inside V2: rejected — interrupts the in-flight campaign and widens V2 scope.

## Security implications

None introduced by the deferral. V2.1 work must preserve fail-closed admission and bounded-cardinality telemetry (no run/session/correlation identifiers as metric labels).

## Operational implications

Until V2.1: maintenance remains a planned-downtime activity; concurrency stays bounded at the current default; callers must handle `429` with backoff.
