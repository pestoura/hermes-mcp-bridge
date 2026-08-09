# ADR-0032 — Integration providers are plugins behind a closed, in-repo allow-list

- Status: Accepted (Phase 7)
- Supersedes: the `ADR-0024` proposal in the downstream design lane, which was
  drafted before ADR-0024..ADR-0031 were taken by accepted Phases 5 and 6.
- Related: ADR-0003 (no generic passthrough), ADR-0017 (capability removal is
  breaking), ADR-0030 (least-privilege computed capabilities).

## Context

Phase 7 opens the gateway to providers beyond GitHub. Ad-hoc provider code in the
executor would make policy, credential handling and audit provider-specific, and
every new integration would re-litigate the fail-closed ordering.

## Decision

Every integration is a provider plugin exposing exactly three operations:
`describe()` (pure, no I/O, no credentials), `health(auth_status)` (bounded
read-only probe receiving credential *status* only) and an execution adapter
invoked by the gateway with a validated request, a request-scoped authorization
handle and a budget.

Providers are resolved from `PROVIDER_ALLOW_LIST`, a tuple in
`v2/provider_manifests.py`. There is no entry-point scanning, no plugin
directory, no network fetch and no dynamic import. An unknown provider id is a
fail-closed refusal (`E-PROVIDER-UNKNOWN`), never an import attempt.

A provider never receives the tool registry, the policy engine, the audit sink,
the credential broker, another provider's authorization or the caller's raw
identity, and never performs policy evaluation, approval checks, idempotency
decisions, audit writes or mode selection.

## Consequences

- The fail-closed ordering is written once, in `v2/provider_gateway.py`, and is
  identical for every provider.
- Removing a provider id from the allow-list is a complete, verifiable rollback
  (layer 1) with zero side effects.
- Third-party plugin ecosystems are explicitly out of scope for V2.
