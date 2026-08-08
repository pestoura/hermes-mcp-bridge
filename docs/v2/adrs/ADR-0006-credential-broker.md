# ADR-0006 — Credential Broker Abstraction

> **V2 · PHASE 1 CONTRACT IMPLEMENTED · NOT YET ACCEPTED · NO IMPACT ON V1**

**Status:** Accepted in principle; Phase 1 implements the interface only, `REGISTRY_ACCEPTED` not declared.

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

## Phase 1 outcome
`CredentialBroker` exists as a `Protocol` returning only
`CredentialCapabilityStatus` (capability ID, provider, readiness state,
version). A single in-memory `StaticCredentialBroker` supports tests. No secret
value, path or environment variable name is exposed by any method or by any
serialization path.

## Open questions
Initial provider backend and credential readiness SLA remain open (OD-005).
Phase 1 deliberately implements **no** real backend.
