# ADR-0015 — Result Shaping and Artifact References

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Large tool responses consume client/LLM context even when only a small projection is needed.

## Decision
Support field selection/count/exists/top-N/metadata-only/pagination and persist oversized results as integrity-protected artifacts referenced by digest/metadata.

## Consequences
Lower returned bytes/tokens; introduces artifact storage/retrieval/retention concerns.

## Alternatives
Always return raw payload; always summarize with LLM.

## Security implications
Secret-aware schemas/redaction and artifact ACL/integrity are required; raw external content remains untrusted.

## Operational implications
Measure raw vs returned bytes and support deterministic aggregation/replay simulation.

## Open questions
Artifact store and retention/expiry defaults.
