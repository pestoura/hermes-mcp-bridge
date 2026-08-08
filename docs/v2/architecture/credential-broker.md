# Credential Broker

> **V2 · PHASE 1 CONTRACT IMPLEMENTED · NOT YET ACCEPTED · NO IMPACT ON V1**

Phase 1 implements the **contract only** (`hermes_mcp_bridge.v2.credentials`).
No real secret backend exists in this phase.

V2 formalizes the Hermes host as a **Credential Broker**. Clients request a capability; they never receive credential material.

```text
Client -> typed operation -> policy -> CredentialProvider -> target backend
```

## Requirements

- least privilege;
- capability-scoped credentials;
- rotation, expiry and revocation;
- health/readiness;
- provenance and audit;
- fail-closed secret redaction;
- no serialization of secrets to clients or result manifests.

Runbooks and tools reference stable capability IDs such as `github.read`, `github.write`, `github.admin`, not token names, paths or environment variables.

## Provider abstraction

A `CredentialProvider` interface should remain independent from storage. Candidate future backends include a restricted file provider, system keyring, Vault and cloud secret managers. V2 must not treat `~/.hermes/.env` as the final generalized secret vault.

## GitHub direction

The audited broad PAT is a hardening finding, not the desired v2 model. Prefer a GitHub App or fine-grained tokens with separate read/write/admin capabilities and narrow repository scope.

## Phase 1 implementation notes

- `CredentialCapabilityStatus` carries only `capability_id`, `provider`,
  `state` (a `CapabilityState`) and `version`. The model forbids extra fields,
  so credential material cannot be smuggled in through an unexpected key, and
  its canonical projection has exactly those four keys.
- `CredentialBroker` is a runtime-checkable `Protocol` exposing `status()` and
  `is_ready()` only. Neither returns credential material, a secret path or an
  environment variable name. An unknown capability yields `None` / `False` —
  fail closed, never a permissive default.
- The only implementation shipped is `StaticCredentialBroker`, an in-memory
  broker for tests and wiring. **No file, environment, keyring or Vault backend
  is implemented in Phase 1** — OD-005 stays open, as does the least-privilege
  GitHub credential model (OD-016 / ADR-0007).
- Tools reference `credential_capability_id` (a stable capability ID) and never
  a token name, path or environment variable. Snapshot and projection
  serialization exclude credential values entirely; projection also excludes
  the capability ID itself.

## Readiness

`configured` is distinct from `healthy` and from `authorized-for-v2`. Credential readiness should validate expiry/refreshability/target authentication without exposing the secret. An expired access token with a valid refresh path may be recoverable but must not be assumed healthy without a check.
