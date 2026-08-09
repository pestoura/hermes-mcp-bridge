# ADR-0033 — Per-provider credential isolation domains

- Status: Accepted (Phase 7)
- Supersedes: the `ADR-0026` proposal in the downstream design lane.
- Related: ADR-0032, Phase 3 least-privilege GitHub write credential split.

## Context

A single broad credential shared across providers makes least privilege
unprovable and turns any provider compromise into a cross-provider one.

## Decision

Each provider owns a credential **domain** containing at most two capability
ids, `<provider>.read` and `<provider>.write`. Resolution is keyed by
`(provider_id, credential_capability_id)`; a request outside the domain is
refused **at the broker** with `E-CRED-CROSS-DOMAIN` and audited. Admin-class or
broad credentials are refused at registration (`broad_credential=true` is never
accepted).

The broker exposes two distinct surfaces: a boolean **status** for the
registry/health path, and an **authorization handle** for the execution
boundary. The handle is single-use, deadline-bound, renders as
`<AuthorizationHandle …redacted>` and raises on `__reduce__`/`__copy__`, so it
cannot be serialized into an audit record, an evidence file or a log line.

Requested scopes must be a subset of the granted scope-set; evidence records the
scope-set **digest**, never material.

## Consequences

- Escalation can never widen credential scope or convert read authority into
  write authority (safety invariant I3), because the widening is refused before
  a handle exists.
- Rotation is a control-plane action requiring no gateway restart: in-flight
  handles complete on the old material or fail closed, never silently retry.
- A revoked domain moves its capabilities to `UNAVAILABLE` on the next probe or
  the first `E-CRED-*` response.
