# Credential Broker

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

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

## Readiness

`configured` is distinct from `healthy` and from `authorized-for-v2`. Credential readiness should validate expiry/refreshability/target authentication without exposing the secret. An expired access token with a valid refresh path may be recoverable but must not be assumed healthy without a check.
