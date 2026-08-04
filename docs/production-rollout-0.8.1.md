# Production rollout and rollback — 0.8.1

Runbook for deploying bridge contract **0.8.1** and rolling back to **0.8.0**.
All tooling lives in `deploy/0.8.1/`. Nothing here prints secrets or `.env`
contents.

## Contract summary

| Item | Value |
| --- | --- |
| Package / bridge / manifest version | `0.8.1` |
| Wire schema version | `0.6.1` (unchanged across 0.8.x) |
| Tool count | **27** (mandatory set from `contracts.TOOL_CONTRACTS["0.8.1"]`) |
| Tool added in 0.8.x | `hermes_readiness` |
| Rollback target | `0.8.0` (27 tools, same schema) |
| Compose project | `hermes-mcp-bridge` (always pinned with `-p`) |

Data migration: **none**. The SQLite schema is `0.6.1` in both 0.8.0 and 0.8.1,
so rollback reverses no migration and touches no state file.

## Safety model

- **Dry-run by default.** `deploy.sh` and `rollback.sh` perform no mutating
  action unless *both* `EXECUTE_DEPLOYMENT=YES` and a matching `EXPECTED_SHA`
  are supplied.
- **Fixed Compose project.** Every `docker compose` call goes through the
  `compose()` helper in `lib.sh`, which always injects
  `-p hermes-mcp-bridge`. A wrong working directory can no longer create a
  parallel project or orphan the production containers.
- **Fail-fast.** Every script runs `set -Eeuo pipefail`; all expansions are
  quoted.
- **Idempotent.** Re-running `deploy.sh` in execute mode when the container is
  already on the candidate image skips recreation and only re-validates. The
  same applies to `rollback.sh`.
- **Image validation.** `preflight.sh` asserts the candidate image carries the
  expected `org.opencontainers.image.revision` label and that the rollback tag
  resolves to the expected image ID.

## Files

| File | Purpose |
| --- | --- |
| `deploy/0.8.1/lib.sh` | shared constants, `compose()` helper, assertions |
| `deploy/0.8.1/preflight.sh` | read-only GO/NO-GO check |
| `deploy/0.8.1/deploy.sh` | backup + candidate rollout (dry-run default) |
| `deploy/0.8.1/rollback.sh` | revert to previous image (dry-run default) |
| `deploy/0.8.1/validate.sh` | read-only post-deploy contract validation |
| `deploy/0.8.1/compose.candidate.yml` | candidate service definition |
| `deploy/0.8.1/compose.rollback.yml` | rollback service definition |

## Environment

Non-secret parameters only; the API key stays in the deployment `.env`, which
these scripts merely reference through `env_file`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CANDIDATE_IMAGE` | `hermes-mcp-bridge:0.8.1-candidate` | candidate tag |
| `ROLLBACK_IMAGE` | `hermes-mcp-bridge:rollback-0.8.0-9c7fc64` | rollback tag |
| `ROLLBACK_IMAGE_ID` | — | expected image ID of the rollback tag |
| `EXPECTED_SHA_0_8_1` | — | SHA expected on the candidate image label |
| `REQUIRED_SHA` | — | SHA that `EXPECTED_SHA` must match to execute |
| `BRIDGE_ENV_FILE` | `/home/estourpm/hermes-mcp-bridge/.env` | deployment env file |
| `BRIDGE_STATE_DIR` | `/home/estourpm/hermes-mcp-bridge/data` | state directory |
| `BACKUP_DIR` | `/home/estourpm/hermes-mcp-bridge-deploy/backups` | SQLite backups |
| `MCP_PORT` | `8765` | bridge port used by `validate.sh` |

## Procedure — rollout

1. **Build and tag** the candidate image with the release SHA in
   `org.opencontainers.image.revision`. (Out of scope for this runbook's
   scripts; they never build.)

2. **Preflight (read-only):**

   ```bash
   EXPECTED_SHA_0_8_1=<sha> ROLLBACK_IMAGE_ID=<sha256:...> \
     ./deploy/0.8.1/preflight.sh
   ```

   Expect `PREFLIGHT: GO`. Any `ABORT:` is a NO-GO — stop.

3. **Dry-run the deploy:**

   ```bash
   REQUIRED_SHA=<sha> EXPECTED_SHA_0_8_1=<sha> ./deploy/0.8.1/deploy.sh
   ```

   Expect `DEPLOY: DRY_RUN OK` and a printed plan. No container is recreated.
   Confirm the printed compose project is `hermes-mcp-bridge`.

4. **Execute:**

   ```bash
   REQUIRED_SHA=<sha> EXPECTED_SHA_0_8_1=<sha> \
   EXECUTE_DEPLOYMENT=YES EXPECTED_SHA=<sha> \
     ./deploy/0.8.1/deploy.sh
   ```

   The script backs up the SQLite state via `sqlite3.backup` (mode `600`),
   recreates the service with the fixed project, waits, then validates.

5. **Validation** runs automatically. To re-run standalone:

   ```bash
   ./deploy/0.8.1/validate.sh
   ```

   Expected assertions: `initialize` responds, `tools=27`,
   `hermes_readiness` present, `bridge_version=0.8.1`,
   `manifest_version=0.8.1`, `schema_version=0.6.1`, `unsupported_tools` empty,
   container `healthy`. Output ends with `VALIDATE: PASS`.

## Procedure — rollback

Trigger when validation fails, the container is unhealthy after settle, or the
tool contract is not satisfied.

1. **Dry-run:**

   ```bash
   REQUIRED_SHA=<sha> ROLLBACK_IMAGE_ID=<sha256:...> ./deploy/0.8.1/rollback.sh
   ```

   Expect `ROLLBACK: DRY_RUN OK`.

2. **Execute:**

   ```bash
   REQUIRED_SHA=<sha> ROLLBACK_IMAGE_ID=<sha256:...> \
   EXECUTE_DEPLOYMENT=YES EXPECTED_SHA=<sha> \
     ./deploy/0.8.1/rollback.sh
   ```

   The rollback recreates the service from `compose.rollback.yml` on the fixed
   project and validates against `bridge_version=0.8.0` with 27 tools.

3. **State.** No restore is required. If a restore is nevertheless desired, the
   pre-deploy backup is the newest `state-*.sqlite3` in `BACKUP_DIR`; stop the
   container before replacing the file.

## Verification checklist

- [ ] `PREFLIGHT: GO`
- [ ] Dry-run printed a plan and made no mutating docker call
- [ ] `docker compose ls` shows only the `hermes-mcp-bridge` project
- [ ] `VALIDATE: PASS` with 27 tools and `hermes_readiness`
- [ ] `hermes_readiness` → `components.tool_contract.missing == []`
- [ ] `schema_version` still `0.6.1`
- [ ] Container `healthy`, `RestartCount` stable

## Known limitations

- ShellCheck is not installed system-wide on the release host. The test suite
  discovers it on `PATH` or next to the active interpreter (`shellcheck-py`),
  runs it at severity `warning`, and skips only when it is truly unavailable.
  Equivalent static checks (fixed Compose project, fail-fast, quoting, dry-run
  non-mutability, `bash -n`) are always enforced by
  `tests/test_rollout_scripts_0_8_1.py`, so the guarantees hold with or without
  ShellCheck.
- The scripts never build images and never publish. Image build and tagging is
  a separate, explicitly authorized step.
