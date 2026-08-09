# Integration Plugin Boundary

>
> **V2 · PHASE 7 · implemented, disabled by default behind `PROVIDER_FEATURE_ENABLED`**
>
> Predecessor gates `DIRECT_MUTATION_ACCEPTED`, `BATCH_ACCEPTED`, `DAG_ACCEPTED`
> and `RUNBOOK_ACCEPTED` are accepted. Only `github` and `jira` are in the
> provider allow-list; every other provider stays `CANDIDATE` or
> `BLOCKED_UNCONFIRMED` and is refused at registration.

## Interface (documentation-level; not code)

A provider plugin exposes exactly three operations:

| Operation | Input | Output | Purity |
|---|---|---|---|
| `describe()` | none | static provider manifest: provider id, version, declared capabilities, required credential capability ids, scope requirements | pure, no I/O, no credentials |
| `health(auth_status)` | credential *status* only (never material) | `READY` / `DEGRADED` / `UNAVAILABLE` + stable reason code | bounded I/O, no mutation |
| `execute(request, authorization, budget)` | typed validated request, request-scoped authorization handle, budget (deadline, byte cap, call cap) | typed result or typed refusal | side effects only via the provider's own client |

## What the boundary denies

A provider **never** receives: the tool registry, the policy engine, the audit
sink, the credential broker, another provider's authorization, the caller
principal's raw identity, or the ability to enqueue further gateway work.

A provider **never** performs: policy evaluation, approval checks, idempotency
decisions, audit writes, retries across the boundary, or mode selection. Those
belong to the gateway and remain identical for all providers.

## Loading

Providers are resolved from an explicit in-repo allow-list keyed by provider id.
No entry-point scanning, no plugin directories, no network fetch, no `eval`.
An unknown provider id is a fail-closed refusal (`E-PROVIDER-UNKNOWN`), never a
dynamic import attempt.

## Isolation and containment

| Concern | Rule |
|---|---|
| Egress | Provider declares an exact host allow-list in its manifest; anything else is refused before the client is constructed |
| Redirects | Disabled; a redirect is `E-PROVIDER-REDIRECT` |
| Proxy inheritance | Environment proxy inheritance disabled, as in the accepted Phase 2 read path |
| Filesystem | No provider reads or writes repository or host paths except an explicitly declared, provider-scoped temp area |
| Subprocesses | Denied by default; a provider needing local execution (`docker`, `systemd`) declares an exact argv template allow-list, no shell |
| Time | Every call carries a deadline; exceeding it is `E-PROVIDER-DEADLINE`, and the operation's mutation class decides whether outcome is unknown |
| Size | Response byte budget enforced before parsing; exceeding it is `E-PROVIDER-RESULT-TOO-LARGE` |
| Faults | A provider exception is normalized to a redacted typed error; provider stack traces never reach the caller or the metric labels |
| Blast radius | Provider failure may degrade only its own capabilities; it may not mark other providers or the gateway unhealthy |

## Versioning

Manifest carries `provider_version` and `contract_version`. A contract-version
mismatch is refused at load, not adapted. Capability removal is a breaking change
requiring a new capability id, per ADR-0017.
