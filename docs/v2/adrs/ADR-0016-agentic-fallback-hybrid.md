# ADR-0016 — Agentic Fallback and HYBRID Escalation

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Some requests genuinely require reasoning, but deterministic failures should not automatically route all context through an LLM.

## Decision
Retain `hermes_prompt` for AGENTIC work. HYBRID performs deterministic steps first and escalates only for explicit reasons such as diagnosis/unknown intent/unsupported tool/complex unstructured analysis, within escalation/token/time/context budgets.

## Consequences
Preserves flexibility while reducing unnecessary LLM use; requires clear escalation policy.

## Alternatives
Always-agentic; Router LLM; never permit LLM fallback.

## Security implications
Minimum-necessary context shaping reduces exposure; agent output never bypasses deterministic policy for mutations.

## Operational implications
Track escalation rate/tokens/reasons and support fallback to v1 during migration.

## Open questions
Confidence thresholds and which escalation reasons are enabled by default.
