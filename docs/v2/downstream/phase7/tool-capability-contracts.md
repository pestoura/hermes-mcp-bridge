# Tool and Capability Contracts per Provider Family

>
> **V2 · PHASE 7 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Predecessor gates `DIRECT_MUTATION_ACCEPTED`, `BATCH_ACCEPTED`, `DAG_ACCEPTED`
> and `RUNBOOK_ACCEPTED` are not accepted. No provider may be registered, wired
> or health-probed against production credentials on the basis of this lane.

All tools reuse the accepted Phase 1 `ToolDefinition` invariants (typed schema,
security tier, mutation class, idempotency class, capability reference) and the
Phase 3 mutation semantics (operation digest, approval binding, idempotency key,
write-ahead audit) without modification.

Contracts below are **candidate** definitions; each becomes real only inside its
own accepted integration gate.

## Email (Google/IMAP-class)

| Tool | Class | Tier | Idempotency | Notes |
|---|---|---|---|---|
| `email.list_messages` | DIRECT_READ | T1 | n/a | Bounded query grammar built by code; caller supplies typed filters (label, date range, sender), never a raw query string |
| `email.get_message` | DIRECT_READ | T1 | n/a | Body returned only under an explicit `include_body` flag with a byte budget; attachments referenced, never inlined |
| `email.send_message` | DIRECT_WRITE | T3 | idempotency-key required | External side effect, irreversible; approval required by default; recipients validated against a policy allow/deny rule |
| `email.modify_labels` | DIRECT_WRITE | T2 | idempotent by target state | Reversible; compensation = restore prior label set captured write-ahead |

Denied in Phase 7: permanent delete, filter/forwarding rule creation, delegation
changes, OAuth grant management.

## Calendar

| Tool | Class | Tier | Idempotency | Notes |
|---|---|---|---|---|
| `calendar.list_events` | DIRECT_READ | T1 | n/a | Time-window bounded; attendee lists redacted unless explicitly requested |
| `calendar.get_event` | DIRECT_READ | T1 | n/a | |
| `calendar.create_event` | DIRECT_WRITE | T2/T3 | idempotency-key required | T3 when it sends invitations to external attendees; external invite requires approval |
| `calendar.update_event` | DIRECT_WRITE | T2/T3 | optimistic concurrency via etag/version | Prior state captured write-ahead for compensation |
| `calendar.delete_event` | DIRECT_WRITE | T3 | idempotency-key required | Approval required; not compensable — recreation is a new event |

## Home Assistant

| Tool | Class | Tier | Idempotency | Notes |
|---|---|---|---|---|
| `homeassistant.list_entities` | DIRECT_READ | T1 | n/a | Projected fields only |
| `homeassistant.get_state` | DIRECT_READ | T1 | n/a | |
| `homeassistant.call_service` | DIRECT_WRITE | T2/T3 | naturally idempotent for absolute-state services | Entity allow-list is exact; wildcard domains denied. Physical-world effect: locks, covers, alarms, water/heating are T3 with approval and are DENY by default until an explicit per-entity decision exists |

## Grafana

| Tool | Class | Tier | Idempotency | Notes |
|---|---|---|---|---|
| `grafana.query_range` | DIRECT_READ | T1 | n/a | Datasource id allow-list; query built from typed selectors; free-form PromQL/LogQL is refused unless a signed, registered query template id is used |
| `grafana.get_dashboard` | DIRECT_READ | T1 | n/a | |
| `grafana.list_alerts` | DIRECT_READ | T1 | n/a | |
| `grafana.silence_alert` | DIRECT_WRITE | T3 | idempotency-key required | Suppresses safety signal: approval mandatory, bounded max duration, auto-expiry, audited with justification reference |

Dashboard/alert-rule mutation is out of scope for Phase 7.

## RITMO

Status: **BLOCKED_UNCONFIRMED**. No contract is specified. Prerequisites before
any design is written: independent host confirmation that RITMO exists and is
reachable; documented API surface and authentication model; data-classification
review; a named owner. Until then RITMO has no capability ids, no credential
domain and no lane in the test matrix.

## Future providers (Cloudflare, Docker, systemd, Jira, n8n)

They inherit this shape. Two additional constraints apply to local-execution
providers (`docker`, `systemd`): exact argv templates with no shell, and a
unit/container name allow-list. `systemd` is user-scope only; anything requiring
elevation is DENY, consistent with the host constraint that non-interactive sudo
is unavailable.
