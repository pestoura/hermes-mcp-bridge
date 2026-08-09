# Compatibility

## 1.0.0 -> 2.0.0

`2.0.0` is a **product/delivery version bump for the V2 programme**, not a
protocol bump. It is fully backward compatible and requires **no client
action**.

| Field | 1.0.0 | 2.0.0 |
| --- | --- | --- |
| `bridge_version` / `manifest_version` | `1.0.0` | `1.0.0` (unchanged) |
| `contract_version` | `1.0.0` | `1.0.0` (unchanged) |
| `schema_version` (wire) | `0.6.1` | `0.6.1` (unchanged) |
| SQLite migration ledger | v10 | v10 (unchanged) |
| Mandatory tools | 27 | 27 (unchanged) |
| `manifest_hash` | — | unchanged (contract identical) |

Consequences:

- Clients caching the capability manifest do **not** need to refresh: the
  manifest is byte-identical to `1.0.0`.
- No tool is added, renamed or removed; no envelope, event or payload field
  changes.
- No SQLite migration runs on upgrade, so downgrade needs no data reversal.
- The V2 runtime (`src/hermes_mcp_bridge/v2/`) stays additive and unwired to the
  V1 tool-registration path; the one-directional boundary (no V1 module imports
  V2) is enforced by the Phase 9 gate check `P9-03`.

Version-reporting rule for this line: the **release** version (`2.0.0`, the Git
tag `v2.0.0`) is deliberately decoupled from the **contract** version
(`1.0.0`), exactly as the wire schema (`0.6.1`) is already decoupled from the
bridge version. Do not "fix" a runtime that reports `1.0.0` on release `2.0.0`
— that is the contract.

Upgrade and rollback: `docs/release-2.0.0.md`. Evidence:
`docs/release-2.0.0-evidence.md`.

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
| 0.8.0 / 0.8.1 / 0.8.2 / 0.9.0 | 27 | `hermes_readiness` |

## 0.8.2 -> 0.9.0

0.9.0 is a **base-image security release**. It changes no client-visible
contract: 27 tools, schema `0.6.1`, no new SQLite migration, `hermes_readiness`
still mandatory. Only `bridge_version`/`manifest_version`/`contract_version`
move to `0.9.0`, which also changes `manifest_hash` — clients that cache the
capability manifest must refresh.

The container base moves from the floating `python:3.11-slim-bookworm` tag to
`python:3.12-slim-trixie` pinned by digest
(`sha256:57cd7c3a...710de`), taking the image from 6 CRITICAL / 20 HIGH to
4 CRITICAL / 19 HIGH under identical scan flags. Rationale, CVE matrix and
rejected alternatives: `docs/base-image-security-0.9.0.md`.

Operational notes:

- The interpreter inside the image is now Python 3.12. `requires-python` stays
  `>=3.11`, so nothing about local development changes.
- systemd is still not installed in the image; the `systemctl`/`systemd-run`
  secret-rotation tests remain host-only and continue to be reported as
  failures in host gates rather than silenced.

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
