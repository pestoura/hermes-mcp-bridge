# Provider Credential Isolation

>
> **V2 · PHASE 7 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Predecessor gates `DIRECT_MUTATION_ACCEPTED`, `BATCH_ACCEPTED`, `DAG_ACCEPTED`
> and `RUNBOOK_ACCEPTED` are not accepted. No provider may be registered, wired
> or health-probed against production credentials on the basis of this lane.

## Domains

Each provider owns a credential **domain**. A domain contains at most two
capability ids: `<provider>.read` and `<provider>.write`. Admin-class
credentials are never provisioned to the gateway.

| Provider | Read capability | Write capability | Never |
|---|---|---|---|
| email | `email.read` | `email.send` | account admin, OAuth grant management, forwarding rules |
| calendar | `calendar.read` | `calendar.write` | domain-wide delegation |
| homeassistant | `homeassistant.read` | `homeassistant.control` | long-lived owner token with full admin, user management |
| grafana | `grafana.read` | `grafana.silence` | org admin, datasource credentials, API key creation |
| github (existing) | `github.read` | `github.write` | `github.admin` |
| ritmo | — | — | blocked pending confirmation |

## Isolation rules

1. Resolution is keyed by `(provider_id, capability_id)`. A provider requesting a
   capability outside its domain is refused **at the broker**
   (`E-CRED-CROSS-DOMAIN`), and the refusal is audited.
2. The broker returns **status** to the registry/health path and a
   **request-scoped authorization handle** only to the execution boundary — never
   raw material to the plugin where a handle suffices.
3. Handles are single-request, deadline-bound, and are not cached by providers.
4. Secrets never appear in manifests, snapshots, capability projections, metric
   labels, audit records, evidence documents, error strings or logs. Evidence
   records only provider, capability id, scope-set digest, credential type and
   `broad_credential=false`.
5. Least privilege is asserted, not assumed: acceptance requires a recorded
   scope-set for each credential and a negative test proving an out-of-scope call
   is refused by the provider *and* would have been refused by policy.

## Rotation and revocation

- Rotation is a control-plane action; a rotated credential must not require a
  gateway restart to take effect, and in-flight requests either complete on the
  old handle or fail closed — never silently retry on the new one.
- Revocation moves the capability to `UNAVAILABLE` on the next probe or on the
  first `E-CRED-*` provider response, whichever is first.
- Rotation drills are part of Phase 9 acceptance (`../phase9/rollback-drills.md`).
