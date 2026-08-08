# ADR-0010 — Skill vs Runbook

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Hermes has many Markdown/context skills, but skill guidance is not equivalent to a deterministic executable workflow.

## Decision
Define Skill as agent knowledge/procedure; define Runbook as typed, executable, versioned, validated, testable, auditable and governed. Permit explicit reviewed `Skill -> Promote -> Runbook` conversion, never automatic bulk conversion.

## Consequences
Known workflows can avoid LLM reasoning while exploratory knowledge remains in skills.

## Alternatives
Treat skills as executable; replace all skills with code.

## Security implications
Promoted runbooks require integrity, schema, policy and supply-chain controls.

## Operational implications
Need registry, versioning, tests, compile step and promotion lifecycle.

## Open questions
Exact runbook DSL and signing/promotion process.
