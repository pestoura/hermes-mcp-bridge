# ADR-0007 — Least-Privilege Credentials

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
The audit observed a broad GitHub PAT. Direct mutation increases the impact of overprivileged credentials.

## Decision
Use capability-scoped least-privilege credentials. Prefer GitHub App or fine-grained tokens and separate read/write/admin capabilities where practical.

## Consequences
More credentials/app permissions and rotation management; reduced blast radius.

## Alternatives
Reuse current broad PAT for all v2 operations.

## Security implications
Directly reduces confused-deputy and credential-compromise impact.

## Operational implications
Credential readiness and per-repository installation/permission management become deployment gates.

## Open questions
GitHub App vs fine-grained token for MVP and exact repository scopes.
