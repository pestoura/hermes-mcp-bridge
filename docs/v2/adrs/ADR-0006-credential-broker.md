# ADR-0006 — Credential Broker Abstraction

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Direct execution needs credentials while clients must never receive them, and current secrets are stored through heterogeneous mechanisms.

## Decision
Introduce a backend-independent `CredentialProvider`/broker addressed by capability IDs such as `github.read` rather than secret names/paths.

## Consequences
Requires health, rotation, expiry, revocation and audit interfaces.

## Alternatives
Read environment variables directly in each tool; standardize immediately on one vault product.

## Security implications
Centralizes secret handling and redaction; broker compromise is a high-value threat requiring least privilege and isolation.

## Operational implications
Allows phased backends (restricted file/keyring/Vault/secret manager) without changing tool contracts.

## Open questions
Initial provider backend and credential readiness SLA.
