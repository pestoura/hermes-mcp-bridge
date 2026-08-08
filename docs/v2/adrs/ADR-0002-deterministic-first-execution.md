# ADR-0002 — Deterministic-First Execution

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Known deterministic operations currently may traverse an unnecessary Hermes LLM/skill path, adding tokens, latency and failure modes.

## Decision
Apply: `DETERMINISTIC WORK -> CODE`, `KNOWN WORKFLOW -> RUNBOOK`, `REASONING -> LLM`. Prefer DIRECT, then BATCH, DAG/RUNBOOK, then AGENTIC. HYBRID is explicit escalation only.

## Consequences
Lower expected token/latency for supported work; requires typed tool coverage and disciplined fallback.

## Alternatives
Always-agentic execution; Router LLM; client-side shell orchestration.

## Security implications
Reduces prompt-injection exposure for deterministic tasks but creates a stronger direct-execution control plane that must be policy hardened.

## Operational implications
Requires benchmarks, capability health and clear unsupported-tool behavior.

## Open questions
Thresholds/criteria for fallback and cost-aware path selection.
