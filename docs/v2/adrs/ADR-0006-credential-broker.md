# ADR-0006 — Credential Broker Abstraction

> **V2 · PHASE 1 CONTRACT ACCEPTED · NO IMPACT ON V1**

**Status:** Accepted for the Phase 1 credential-capability/broker contract only. `REGISTRY_ACCEPTED` evidence validated on integrated `main` commit `4bc999084b88cc5ef5346f21c9f2e09717c63568`.

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
canonical serialization path. This contract was included in the accepted Phase
1 evidence indexed in `docs/v2/evidence/README.md`.

## Open questions
Initial provider backend and credential readiness SLA remain open (OD-005).
Phase 1 deliberately implements **no** real backend. The least-privilege GitHub
credential model remains a Phase 2 prerequisite (OD-016 / ADR-0007), and
`REGISTRY_ACCEPTED` must not be interpreted as provider authorization.
