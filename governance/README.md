# Release maintenance governance

Hermes MCP Bridge adopts JDS-002 as a pilot for release maintenance and post-baseline change governance.

## Invariants

- Bridge `1.0.0` is an accepted public contract identity and is not silently mutated into a different release.
- A functional change after candidate evidence requires a new candidate identity.
- A functional change after GA requires a new release identity.
- Repository acceptance, artifact promotion, runtime deployment and live verification are separate states.
- A merge never implies live support.
- Validation failures become canonical observations and, when remediation is required, canonical `CHG-BRIDGE-*` records.
- Non-blocking improvements may be deferred instead of extending the current release indefinitely.

## Workflow

```text
candidate
  -> validation campaign
  -> observation
  -> ChangeRecord when remediation is needed
  -> short-lived change branch
  -> proportional product gates
  -> revalidation
  -> new candidate/release identity when functional content changed
  -> promotion
  -> live verification
```

## Product-specific authority

JDS-002 governs the record shape and generic lifecycle. Existing Bridge product gates remain authoritative, including V1 contract preservation, policy/HMAC fail-closed behaviour, Phase acceptance gates, supply-chain evidence, rollout safety and runtime proof.

The central JDS-002 implementation is pinned in `governance/jds-002.yml`. The pin must not be moved without review of schema/policy compatibility.
