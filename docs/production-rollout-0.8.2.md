# Production rollout — 0.8.2

All tooling lives in `deploy/0.8.2/`. Nothing here prints secrets, `.env`
contents or container `Env`.

0.8.2 is a **patch release with no client-visible contract change**: 27 tools,
wire schema `0.6.1`, `hermes_readiness` mandatory, no new SQLite migration. The
entire change is in how the rollout decides that a container is healthy.

## Why this release exists — the 0.8.1 false rollback

The 0.8.1 rollout scripts did this:

```bash
compose "$COMPOSE_FILE" up -d
sleep "$SETTLE_SECONDS"      # default 12
bash "$HERE/validate.sh"
```

The container healthcheck is:

| Field | Value |
| --- | --- |
| `start_period` | 10s |
| `interval` | 30s |
| `timeout` | 5s |
| `retries` | 3 |

Docker records **no** health probe result during `start_period`, and the first
real probe lands at roughly 10s, with subsequent probes every 30s. The status is
therefore still `starting` at 12s. `validate.sh` then read
`.State.Health.Status`, saw a value that was not `healthy`, and aborted — on a
container that was fine.

The consequence was a **false rollback signal** during a good deployment. The
0.8.1 rollout only completed because the operator overrode `SETTLE_SECONDS=60`
by hand. That is a magic number tuned to one machine, not a fix.

## The 0.8.2 fix — derived budget plus bounded polling

Two functions in `deploy/0.8.2/lib.sh`.

### `health_settle_budget <container>`

Reads the container's own healthcheck configuration and computes:

```
budget = start_period + (interval + timeout) * retries + HEALTH_SETTLE_MARGIN_SECONDS
```

clamped to `[HEALTH_SETTLE_MIN_SECONDS, HEALTH_SETTLE_MAX_SECONDS]`.

For the production healthcheck: `10 + (30 + 5) * 3 + 15 = 130s`.

If the container declares no healthcheck fields, the `HEALTH_FALLBACK_*`
defaults reproduce the same 130s rather than guessing something shorter.

### `wait_for_health <container> [budget]`

Polls `.State.Health.Status` every `HEALTH_POLL_INTERVAL_SECONDS` (default 2s)
until a terminal condition.

## Health criteria

| Docker health status | Inside budget | Decision |
| --- | --- | --- |
| `healthy` | — | **PASS**, return immediately |
| `unhealthy` | — | **FAIL**, return immediately (no waiting out the budget) |
| `starting` | yes | keep waiting — **explicitly not a failure** |
| `starting` | no (budget expired) | **FAIL** with a timeout message |
| no healthcheck declared | — | warn, pass — unless `HEALTH_REQUIRE_HEALTHCHECK=1`, then FAIL |
| container missing | — | **FAIL** |

`deploy.sh`, `rollback.sh` and `validate.sh` all use this same function, so a
rollback can no longer be declared failed while its replacement container is
legitimately starting.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `HEALTH_POLL_INTERVAL_SECONDS` | `2` | poll cadence |
| `HEALTH_SETTLE_MARGIN_SECONDS` | `15` | slack added to the derived budget |
| `HEALTH_SETTLE_MIN_SECONDS` | `30` | floor, protects against tiny healthchecks |
| `HEALTH_SETTLE_MAX_SECONDS` | `300` | ceiling, bounds a stuck rollout |
| `HEALTH_SETTLE_SECONDS` | *(unset)* | hard override of the derived budget |
| `HEALTH_FALLBACK_*` | 10/30/5/3 | used only when no healthcheck is declared |
| `HEALTH_REQUIRE_HEALTHCHECK` | `0` (`1` in deploy/rollback) | fail closed when no healthcheck is declared |

**Do not pass `SETTLE_SECONDS`.** It no longer exists. Passing
`HEALTH_SETTLE_SECONDS` is a deliberate override and should be justified.

## Files

| Path | Role |
| --- | --- |
| `deploy/0.8.2/lib.sh` | shared constants, `compose()` helper, assertions, health logic |
| `deploy/0.8.2/preflight.sh` | read-only GO/NO-GO check |
| `deploy/0.8.2/deploy.sh` | backup + candidate rollout (dry-run default) |
| `deploy/0.8.2/rollback.sh` | revert to previous image (dry-run default) |
| `deploy/0.8.2/validate.sh` | read-only post-deploy contract validation |
| `deploy/0.8.2/compose.candidate.yml` | candidate service definition |
| `deploy/0.8.2/compose.rollback.yml` | rollback service definition |

## Safety contract (unchanged from 0.8.1)

- Every `docker compose` invocation is pinned with `-p hermes-mcp-bridge`.
- Mutation requires **both** `EXECUTE_DEPLOYMENT=YES` and an `EXPECTED_SHA`
  matching the release SHA. Either alone stays dry-run.
- Deployment is idempotent: a container already on the candidate image is not
  recreated, only re-validated.
- The SQLite state file is backed up via the `sqlite3` `.backup` API (dir 700,
  file 600) before any recreation. Rollback never touches SQLite: schema `0.6.1`
  is identical on both sides.

## Procedure

1. Build and tag the candidate at the release SHA with OCI labels:

   ```bash
   docker build -t hermes-mcp-bridge:0.8.2-<shortsha>-candidate \
     --label org.opencontainers.image.version=0.8.2 \
     --label org.opencontainers.image.revision=<sha> .
   ```

2. Preflight (read-only):

   ```bash
   EXPECTED_SHA_0_8_2=<sha> CANDIDATE_IMAGE=<tag> ./deploy/0.8.2/preflight.sh
   ```

3. Dry-run and read the printed plan:

   ```bash
   CANDIDATE_IMAGE=<tag> ./deploy/0.8.2/deploy.sh
   ```

4. Apply:

   ```bash
   EXECUTE_DEPLOYMENT=YES REQUIRED_SHA=<sha> EXPECTED_SHA=<sha> \
     CANDIDATE_IMAGE=<tag> ./deploy/0.8.2/deploy.sh
   ```

   The script backs up SQLite, recreates the service, waits for health with the
   derived budget, then validates. No manual settle override is required.

5. Independent validation:

   ```bash
   ./deploy/0.8.2/validate.sh
   ```

6. Rollback if needed (same two gates):

   ```bash
   EXECUTE_DEPLOYMENT=YES REQUIRED_SHA=<sha> EXPECTED_SHA=<sha> \
     ROLLBACK_IMAGE=hermes-mcp-bridge:rollback-0.8.1-a3c8c11 \
     ROLLBACK_IMAGE_ID=<sha256:...> ./deploy/0.8.2/rollback.sh
   ```

## Log sanitisation

The health loop prints only the status token and elapsed/budget seconds. It
never runs `docker inspect --format '{{json .Config}}'` and never reads
`.Config.Env`. A test asserts the absence of both from `lib.sh` and scans the
captured output for credential-shaped tokens.
