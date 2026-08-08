# ADR-0009 — DAG Execution Semantics

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Some deterministic workflows have dependencies between tool outputs and later inputs.

## Decision
Support explicit acyclic graphs with schema-validated typed bindings, cycle detection, topological scheduling, independent-branch parallelism, deadlines/cancellation, checkpoints/resume and deterministic transformation nodes without arbitrary eval.

## Consequences
Enables complex deterministic orchestration; requires durable state and binding semantics.

## Alternatives
Agent plans every dependency; runbooks only; unsafe expression/template language.

## Security implications
Typed bindings and budgets mitigate injection/amplification; durable state must preserve scope and plan digest.

## Operational implications
Requires queue/store, execution leases/heartbeats and manual-intervention state.

## Open questions
Exact DAG/transform DSL and durable queue/store technology.
