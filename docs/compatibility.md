# Compatibility

## 0.8.0 → 0.8.1

### Client compatibility

- No breaking change. The 0.8.x tool set is unchanged: 27 tools, including
  `hermes_readiness`.
- Wire schema stays `0.6.1`. No envelope, event or payload field is renamed or
  removed.
- `hermes_health` and `hermes_capabilities` report `bridge_version` and
  `manifest_version` `0.8.1`; `manifest_hash` changes accordingly and clients
  that cache manifests must refresh.
- `hermes_readiness` gains additive fields: `contract_version`,
  `schema_version` and the `components.tool_contract` block. Old clients ignore
  unknown fields.

### Tool contract policy

- The mandatory tool set per version is declared in
  `src/hermes_mcp_bridge/contracts.py` (`TOOL_CONTRACTS`).
- Validation rule: a deployment is valid when **no mandatory tool is missing**.
  Missing tools are a hard failure. Extra (undeclared) tools are reported as a
  warning, because additive tools are permitted within a contract line.
- Counts are always derived from the required set; no caller hard-codes 26 or 27.

| Contract | Tools | Adds |
| --- | --- | --- |
| 0.6.0 / 0.6.1 | 26 | — |
| 0.8.0 / 0.8.1 / 0.8.2 | 27 | `hermes_readiness` |

## 0.8.1 -> 0.8.2

0.8.2 is a patch release. It changes **no** client-visible contract: 27 tools,
schema `0.6.1`, no new SQLite migration, `hermes_readiness` still mandatory.
Only `bridge_version`/`manifest_version`/`contract_version` move to `0.8.2`.

The entire change is operational: the rollout scripts no longer sleep a fixed
number of seconds before validating. See the "Health criteria" section of
`docs/production-rollout-0.8.2.md`.

### Upgrade path

1. Build and tag the 0.8.2 candidate image with the release SHA in
   `org.opencontainers.image.revision`.
2. Run `deploy/0.8.2/preflight.sh` (read-only).
3. Run `deploy/0.8.2/deploy.sh` in dry-run and review the printed plan.
4. Re-run with `EXECUTE_DEPLOYMENT=YES EXPECTED_SHA=<sha>` to apply. Do **not**
   pass `SETTLE_SECONDS`; the health budget is derived from the container's own
   healthcheck.
5. `deploy/0.8.2/validate.sh` waits for `health=healthy` and then asserts 27
   tools, `hermes_readiness` present, `bridge_version` 0.8.2 and
   `schema_version` 0.6.1.
6. Roll back with `deploy/0.8.2/rollback.sh` (same two gates, same health
   logic). No state migration is reversed.

## 0.3.0 → 0.4.0

### Client compatibility

- Existing 0.3.0 clients can continue to call the 7 original tools without changes.
- New tools `hermes_capabilities` and `hermes_agent_card` are additive.
- Tool outputs gain an optional `envelope` field; old clients ignore unknown fields.
- `hermes_health` gains additional bridge discovery fields without removing existing ones.

### Upgrade path

1. Deploy `0.4.0` alongside `0.3.0` behavior.
2. Verify `hermes_health` returns `schema_version: "0.4.0"`, `manifest_version`, and `manifest_hash`.
3. Confirm old tool outputs still contain `session_id`, `execution_id`, `status`, `output`, `error`, and `metadata`.
4. Update clients to consume `hermes_capabilities` and `hermes_agent_card` when ready.
5. Cache manifests and monitor for `manifest_hash_divergence` in health responses.

### Deprecation policy

- Bridge contract versions are deprecated only by explicit changelog entry.
- Old fields remain present for at least one minor release after deprecation.
- New optional fields never remove or rename existing fields in the same release.
