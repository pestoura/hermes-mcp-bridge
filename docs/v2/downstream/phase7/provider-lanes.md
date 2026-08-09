# Per-Provider Lanes, Prerequisites and Risk

>
> **V2 · PHASE 7 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Predecessor gates `DIRECT_MUTATION_ACCEPTED`, `BATCH_ACCEPTED`, `DAG_ACCEPTED`
> and `RUNBOOK_ACCEPTED` are not accepted. No provider may be registered, wired
> or health-probed against production credentials on the basis of this lane.

Each integration has its own gate. Acceptance of one provider grants nothing to
another.

| Provider | Read | Write | Blast radius | Key prerequisite | Gate id (proposed) |
|---|---|---|---|---|---|
| Email | list/get | send, labels | External communication, irreversible | Least-privilege OAuth scope split; recipient policy | `INTEGRATION_EMAIL_ACCEPTED` |
| Calendar | list/get | create/update/delete | External invitations, third-party time | Separate write scope; external-attendee approval rule | `INTEGRATION_CALENDAR_ACCEPTED` |
| Home Assistant | entities/state | call_service | Physical world (locks, heating, alarm) | Exact entity allow-list; safety-critical entity DENY list | `INTEGRATION_HA_ACCEPTED` |
| Grafana | query/dashboard/alerts | silence | Suppression of safety signal | Registered query templates; bounded silence duration | `INTEGRATION_GRAFANA_ACCEPTED` |
| RITMO | — | — | Unknown | Independent confirmation that it exists, with API and data classification | `BLOCKED_UNCONFIRMED` |
| Cloudflare | zone/dns read | deferred | Public DNS/edge exposure | Token scoped to one zone | `INTEGRATION_CF_ACCEPTED` |
| Docker | inspect/list | deferred | Host workloads | argv allow-list, no shell | `INTEGRATION_DOCKER_ACCEPTED` |
| systemd (user) | status/list | restart (later) | Local services | User scope only; no elevation | `INTEGRATION_SYSTEMD_ACCEPTED` |
| Jira | issue read | transition/comment (later) | Organisational records | Project allow-list | `INTEGRATION_JIRA_ACCEPTED` |
| n8n | workflow read | activate (later) | Automation cascade | Workflow id allow-list | `INTEGRATION_N8N_ACCEPTED` |

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
