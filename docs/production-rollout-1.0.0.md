# Hermes MCP Bridge 1.0.0 — controlled production rollout

## Status

This bundle controls both the initial `1.0.0` production rollout and a later exact-SHA refresh of an already-running accepted `1.0.0` candidate.

It never authorizes production deployment by itself. Mutation remains gated by exact image/revision evidence, an explicit rollback baseline, the dual execution gate and post-deploy validation.

For the initial migration from `0.9.0`, the known-good `0.9.0` image remains the rollback baseline. For a subsequent `1.0.0 -> 1.0.0` candidate refresh, the currently running healthy `1.0.0` image may be used as the rollback baseline only when its exact immutable image ID and version are explicitly supplied and the preflight proves that production is running that exact image.

## Invariants

- candidate bridge/manifest/contract: `1.0.0`;
- tool surface: exactly 27 tools;
- wire and SQLite schema: `0.6.1`;
- candidate OCI revision: exact accepted Git SHA;
- rollback: exact immutable image ID plus explicit expected Bridge version;
- default rollback version remains `0.9.0` for the initial rollout;
- same-version refresh requires explicit `ROLLBACK_BRIDGE_VERSION=1.0.0` and does not weaken validation;
- API and HMAC credentials: secret files only;
- policy: file-backed, valid and fail-closed for unknown actions;
- metrics may remain in their separately accepted loopback-only state;
- tracing export, retry and circuit breaker remain disabled unless separately promoted;
- state backup: online SQLite backup plus integrity check;
- restore proof: real isolated restore before container replacement;
- all scripts: dry-run unless both execution gates match.

## Required evidence

Before preflight, record without exposing secrets:

```text
REQUIRED_SHA=<full integrated 1.0.0 SHA>
CANDIDATE_IMAGE=<candidate tag>
CANDIDATE_IMAGE_ID=<immutable Docker image ID>
ROLLBACK_IMAGE=<exact accepted rollback tag/reference>
ROLLBACK_IMAGE_ID=<immutable rollback image ID>
ROLLBACK_BRIDGE_VERSION=<expected version of rollback image>
SBOM_FILE=<retained CycloneDX JSON>
SBOM_SHA256=<64-character digest>
```

The candidate image must carry OCI labels:

```text
org.opencontainers.image.version=1.0.0
org.opencontainers.image.revision=<REQUIRED_SHA>
org.opencontainers.image.source=https://github.com/pestoura/hermes-mcp-bridge
```

The rollback image must match both `ROLLBACK_IMAGE_ID` and `ROLLBACK_BRIDGE_VERSION`. A version string alone is not rollback evidence.

## Initial rollout versus candidate refresh

### Initial `0.9.0 -> 1.0.0`

Use the exact known-good `0.9.0` image as rollback baseline. `ROLLBACK_BRIDGE_VERSION` may remain at its default `0.9.0` value.

### Controlled `1.0.0 -> 1.0.0` refresh

Use this path when production already runs an accepted `1.0.0` image but a later exact Git SHA must be deployed, for example to deliver a repository fix that is not present in the live OCI revision.

Required rules:

1. identify the exact currently running image ID and OCI version read-only;
2. retain that exact image locally as the rollback baseline;
3. set `ROLLBACK_IMAGE`, `ROLLBACK_IMAGE_ID` and explicitly set `ROLLBACK_BRIDGE_VERSION=1.0.0`;
4. build the replacement candidate from the exact accepted `REQUIRED_SHA`;
5. retain/validate its CycloneDX SBOM and provenance evidence;
6. run preflight and dry-run before any mutation;
7. use the normal dual mutation gate only after the exact inputs are reconciled;
8. on rollback to a `1.0.0` baseline, require the full `1.0` security posture again.

Do not downgrade to `0.9.0` merely to satisfy a historical rollout assumption when the current healthy `1.0.0` image itself is the intended known-good rollback baseline. Conversely, do not use an arbitrary historical `1.0.0` image: the preflight requires production to be running the exact supplied rollback image ID before candidate replacement.

## Secret migration prerequisite

The transitional raw API key used only for old rollback paths is no longer allowed.

Before the `1.0.0` preflight:

1. ensure both protected API/HMAC files exist with mode `0400` or `0600` and bridge UID ownership;
2. remove any non-empty `HERMES_API_KEY` assignment from the runtime env file;
3. keep a non-secret `HERMES_BRIDGE_HMAC_KEY_ID`;
4. remove any previous-HMAC file and previous-key metadata for the base rollout.

Do not print the env file or either secret.

## Security/SBOM preflight

`preflight.sh` is read-only and requires:

- candidate OCI version/revision match;
- rollback image reference, immutable ID and expected version match;
- production currently healthy and running that exact rollback image ID;
- retained CycloneDX SBOM with matching SHA-256 and non-empty components;
- SQLite `quick_check=ok` and migration metadata present;
- raw API key absent;
- current API/HMAC secret files valid and different;
- policy file valid with `unknown_action_decision=DENY`;
- no previous-HMAC verifier in the base rollout;
- tracing/retry/circuit features not implicitly enabled;
- remote metrics exposure prohibited;
- at least 5 GiB free by default;
- both Compose files parse under the fixed project name.

Required marker:

```text
HERMES_BRIDGE_1_0_0_PREFLIGHT_GO
```

## Dry-run

`deploy.sh` requires `REQUIRED_SHA` even in dry-run. Without both mutation gates, it only validates and prints the planned operations:

```text
EXECUTE_DEPLOYMENT != YES
or
EXPECTED_SHA != REQUIRED_SHA
```

Required marker:

```text
DEPLOY_1_0_0: DRY_RUN OK
```

## Mutation gates

A production mutation requires both:

```text
EXECUTE_DEPLOYMENT=YES
EXPECTED_SHA=<exact REQUIRED_SHA>
```

The deployment then:

1. repeats the full preflight;
2. creates an atomic `0600` SQLite backup through the SQLite backup API;
3. runs `integrity_check` on the backup;
4. restores the backup to a temporary isolated database;
5. proves integrity, migration metadata and cleanup;
6. retains only the backup digest as separate sanitized evidence;
7. replaces the container through the fixed Compose project;
8. waits for Docker health using a derived bounded budget;
9. confirms the immutable candidate image ID;
10. performs read-only MCP initialize, tool-list, health and readiness calls;
11. reconfirms SQLite quick-check.

Successful completion marker:

```text
HERMES_BRIDGE_1_0_0_PRODUCTION_PASS
```

This marker is deployment evidence, not permission to skip required runtime acceptance such as approval-path or single-slot checks.

## Post-deploy validation

`validate.sh` creates no Hermes run or approval. It proves:

- Docker health is `healthy`;
- MCP initialize succeeds;
- exactly 27 tools are exposed and `hermes_readiness` is present;
- bridge, manifest and contract are `1.0.0`;
- schema remains `0.6.1`;
- upstream is healthy and unsupported-tools is empty;
- policy and current HMAC are file-backed and ready;
- previous HMAC state is absent, not pending/active/expired;
- metrics state matches the accepted configuration and is not remotely exposed;
- tracing export, retry and circuit breaker remain off unless separately promoted;
- restart count is observed without exposing container environment.

## Rollback

Rollback is dry-run by default and targets the exact immutable baseline identified by:

```text
ROLLBACK_IMAGE
ROLLBACK_IMAGE_ID
ROLLBACK_BRIDGE_VERSION
```

For the initial rollout this is normally the known-good `0.9.0` image. For an accepted `1.0.0` candidate refresh it may be the exact pre-refresh `1.0.0` image.

Rollback uses the same API/HMAC secret files and policy file; no raw API key is restored. SQLite is not reverted because schema remains `0.6.1`; the pre-deploy backup is retained for evidence and emergency recovery only.

Mutation requires the same two execution gates. Required marker:

```text
ROLLBACK_1_0_0: PASS
```

After rollback, validate:

- exact rollback image ID;
- declared rollback Bridge version;
- 27 tools;
- schema `0.6.1`;
- upstream and Docker health;
- full `1.0` security posture whenever the rollback baseline version is `1.0.0`.

## Rollout order

1. reconcile and pin the exact accepted repository SHA;
2. build and scan an immutable candidate;
3. retain and hash the CycloneDX SBOM and provenance evidence;
4. perform isolated candidate acceptance;
5. identify and pin the exact currently running rollback baseline;
6. complete any prerequisite Hermes/RITMO acceptance required by the release;
7. record the required production authorization/approval through the normal audited mechanism;
8. run preflight;
9. run dry-run;
10. execute controlled deployment;
11. validate exact running image/revision and runtime contract;
12. perform the specific runtime acceptance that motivated the refresh.

A candidate refresh must never convert repository `GREEN` into runtime `GREEN` by inference. The live OCI revision and the affected runtime behaviour must both be proven after deployment.
