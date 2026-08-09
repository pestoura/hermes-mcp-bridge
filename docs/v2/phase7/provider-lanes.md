# Per-Provider Lanes, Prerequisites and Risk

>
> **V2 · PHASE 7 · implemented, disabled by default behind `PROVIDER_FEATURE_ENABLED`**
>
> Predecessor gates `DIRECT_MUTATION_ACCEPTED`, `BATCH_ACCEPTED`, `DAG_ACCEPTED`
> and `RUNBOOK_ACCEPTED` are accepted. Only `github` and `jira` are in the
> provider allow-list; every other provider stays `CANDIDATE` or
> `BLOCKED_UNCONFIRMED` and is refused at registration.

Each integration has its own gate. Acceptance of one provider grants nothing to
another.

| Provider | Status | Read | Write | Gate id | Evidence basis |
|---|---|---|---|---|---|
| GitHub | **ACCEPTED** | repo/pr/checks | deferred to Phase 3 governed merge | `INTEGRATION_GITHUB_ACCEPTED` | Reference implementation accepted in Phases 2–3; credential domain split already accepted (`github.read` / `github.write`) |
| Jira | **ACCEPTED (read-only)** | issue/project | **not granted** | `INTEGRATION_JIRA_ACCEPTED` | Jira Cloud API credential present on the host and verified by a bounded authenticated read. The credential is shared with other host automations, so a write capability on it would breach least privilege — the write lane is deliberately absent, not merely unimplemented |
| Email | CANDIDATE | list/get | send, labels | — | No dedicated least-privilege OAuth scope split provisioned; the available Google identity is broad and shared |
| Calendar | CANDIDATE | list/get | create/update/delete | — | Same shared broad Google identity; no separate write scope |
| Home Assistant | CANDIDATE | entities/state | call_service | — | No per-entity allow-list decision and no scoped token; physical-world blast radius |
| Grafana | CANDIDATE | query/dashboard/alerts | silence | — | No registered query templates, no scoped API key |
| Cloudflare | CANDIDATE | zone read | deferred | — | The available token verifies for zone listing but is refused (403) on the DNS/tunnel surface; the contract is therefore not established |
| Docker | CANDIDATE | inspect/list | deferred | — | Requires an argv template allow-list not yet defined |
| systemd (user) | CANDIDATE | status/list | deferred | — | User scope only; no elevation available on this host |
| n8n | CANDIDATE | workflow read | deferred | — | No workflow id allow-list |
| RITMO | **BLOCKED_UNCONFIRMED** | — | — | — | Existence, API surface, authentication model and data classification unconfirmed. No capability id, no credential domain, no test-matrix lane |

`ACCEPTED` means the provider is in `PROVIDER_ALLOW_LIST` and its manifest
registers. `CANDIDATE` means the shape is designed but the credential/contract
prerequisite is not evidenced on this host; the provider id is **not** in the
allow-list and registration fails closed. `BLOCKED_UNCONFIRMED` is stronger: no
design is written at all.

Recommended sequence: **read-only first for every provider**, then write for the
lowest-blast-radius provider (Calendar or Jira), then Email send, then Home
Assistant control, then Grafana silence. Nothing here overrides the
Controller-owned roadmap ordering; it is a proposal.

## Provider-specific threat deltas

- **Email:** content-injection from message bodies into downstream reasoning;
  exfiltration by send; recipient spoofing. Mitigation: bodies are data, never
  instructions; recipient policy; approval on send; no auto-reply capability.
- **Calendar:** silent third-party notification; timezone/recurrence ambiguity
  causing unintended mass updates. Mitigation: recurrence expansion is explicit,
  bulk update refused.
- **Home Assistant:** physical harm; automation loops. Mitigation: entity
  allow-list, safety DENY list, rate limit per entity, no `homeassistant.*`
  wildcard services.
- **Grafana:** alert silencing hides incidents. Mitigation: bounded duration,
  approval, audit with justification, silence expiry alarm.
- **Local providers:** argv injection, privilege escalation. Mitigation: no
  shell, exact templates, user scope.
