# Phase 7 — Additional Integrations (design lane)

>
> **V2 · PHASE 7 · implemented, disabled by default behind `PROVIDER_FEATURE_ENABLED`**
>
> Predecessor gates `DIRECT_MUTATION_ACCEPTED`, `BATCH_ACCEPTED`, `DAG_ACCEPTED`
> and `RUNBOOK_ACCEPTED` are accepted. Only `github` and `jira` are in the
> provider allow-list; every other provider stays `CANDIDATE` or
> `BLOCKED_UNCONFIRMED` and is refused at registration.

## Scope

Define, once, how *any* non-GitHub provider joins the V2 execution gateway:
plugin boundary, typed Tool/Capability contracts, capability discovery for
direct-read and direct-write, credential isolation, audit obligations and
fail-closed policy — then apply that shape to Email, Calendar, Home Assistant,
RITMO, Grafana and future providers.

GitHub remains the reference implementation; Phase 7 must not special-case it.

## Documents

| Document | Scope |
|---|---|
| `plugin-boundary.md` | Provider plugin interface, isolation, lifecycle, failure containment |
| `tool-capability-contracts.md` | Typed Tool/Capability contracts per provider family |
| `capability-discovery.md` | Declarative manifests, direct-read/direct-write classification, readiness |
| `credential-isolation.md` | Per-provider credential domains, scopes, rotation, non-crossing |
| `audit-and-policy.md` | Fail-closed ordering, reason codes, audit record, redaction |
| `provider-lanes.md` | Per-provider status, prerequisites, risk and gate |
| `test-matrix.md` | Required positive/negative/adversarial tests per provider |
| `acceptance-criteria.md` | Fail-closed per-integration acceptance criteria |

## Non-goals

- No generic shell/HTTP passthrough provider (contradicts ADR-0003).
- No dynamic or remote plugin loading.
- No multi-provider transactional semantics (Phase 5 DAG owns composition).
- No assumption that RITMO exists.
