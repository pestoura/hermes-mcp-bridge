# ADR-0018 — Observability and Token Economics

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
The value of deterministic execution must be demonstrated with correctness, latency, result-size and token evidence rather than assumptions.

## Decision
Instrument execution modes/nodes, queue/policy/approval/credential/backend latency, result reduction, LLM tokens/escalations and W3C Trace Context. Establish Phase-0 representative v1 baselines before claiming savings.

## Consequences
Measurable economics and reliability; telemetry design must control cardinality/sensitive data.

## Alternatives
Rely on anecdotal token counts; measure only aggregate latency.

## Security implications
Never record secrets, passwords, full prompts or unnecessary sensitive personal data in labels/spans.

## Operational implications
Define SLOs/benchmarks and percentage-without-Hermes-LLM/estimated-token-saved indicators.

## Open questions
Exact SLO targets and token-savings estimation formula.
