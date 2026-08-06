# Hermes MCP Bridge 1.0.0 — controlled production rollout

## Status

This bundle prepares a candidate and rollback path. It does not authorize
production deployment by itself.

Production remains on the known-good `0.9.0` image until all development,
security, isolated acceptance and Hermes/RITMO single-slot gates are complete.

## Invariants

- candidate bridge/manifest/contract: `1.0.0`;
- tool surface: exactly 27 tools;
- wire and SQLite schema: `0.6.1`;
- rollback: exact immutable `0.9.0` image ID;
- API and HMAC credentials: secret files only;
- policy: file-backed, valid and fail-closed for unknown actions;
- metrics, tracing export, retry and circuit breaker: disabled;
- previous HMAC verifier: absent for the base rollout;
- state backup: online SQLite backup plus integrity check;
- restore proof: real isolated restore before container replacement;
- all scripts: dry-run unless both execution gates match.

## Required evidence

Before preflight, record without exposing secrets:

```text
REQUIRED_SHA=<full integrated 1.0.0 SHA>
CANDIDATE_IMAGE=<candidate tag>
CANDIDATE_IMAGE_ID=<immutable Docker image ID>
ROLLBACK_IMAGE=<known-good 0.9.0 tag/reference>
ROLLBACK_IMAGE_ID=<immutable 0.9.0 Docker image ID>
SBOM_FILE=<retained CycloneDX JSON>
SBOM_SHA256=<64-character digest>
```

The candidate image must carry OCI labels:

```text
org.opencontainers.image.version=1.0.0
org.opencontainers.image.revision=<REQUIRED_SHA>
org.opencontainers.image.source=https://github.com/pestoura/hermes-mcp-bridge
```

## Secret migration prerequisite

The transitional raw API key used only for rollback to `0.8.2` is no longer
allowed.

Before the `1.0.0` preflight:

1. confirm the known-good `0.9.0` rollback image supports
   `HERMES_API_KEY_FILE` and `HERMES_BRIDGE_HMAC_SECRET_FILE`;
2. ensure both protected files exist with mode `0400` or `0600` and bridge UID
   ownership;
3. remove any non-empty `HERMES_API_KEY` assignment from the runtime env file;
4. keep a non-secret `HERMES_BRIDGE_HMAC_KEY_ID`;
5. remove any previous-HMAC file and previous-key metadata for the base rollout.

Do not print the env file or either secret.

## Security/SBOM preflight

`preflight.sh` is read-only and requires:

- candidate OCI version/revision match;
- rollback image reference and ID match;
- production currently healthy and running that exact rollback ID;
- retained CycloneDX SBOM with matching SHA-256 and non-empty components;
- SQLite `quick_check=ok` and migration metadata present;
- raw API key absent;
- current API/HMAC secret files valid and different;
- policy file valid with `unknown_action_decision=DENY`;
- no previous-HMAC verifier in the base rollout;
- optional capabilities disabled;
- at least 5 GiB free by default;
- both Compose files parse under the fixed project name.

Required marker:

```text
HERMES_BRIDGE_1_0_0_PREFLIGHT_GO
```

## Dry-run

`deploy.sh` requires `REQUIRED_SHA` even in dry-run. Without both mutation
gates, it only validates and prints the planned operations:

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

This marker may only be accepted after the mandatory single-slot gate is also
complete. Running the script before that decision is prohibited by the release
process even if the technical mutation gates are provided.

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
- metrics exporter and tracing export are off;
- retry and circuit breaker are off and cannot affect mutations/SSE;
- restart count is observed without exposing container environment.

## Rollback

Rollback is dry-run by default and targets the exact immutable `0.9.0` image.
It uses the same API/HMAC secret files and policy file; no raw API key is
restored.

SQLite is not reverted because schema remains `0.6.1`. The pre-deploy backup is
retained for evidence and emergency recovery only.

Mutation requires the same two execution gates. Required marker:

```text
ROLLBACK_1_0_0: PASS
```

After rollback, validate:

- exact `0.9.0` image ID;
- bridge/manifest `0.9.0`;
- 27 tools;
- schema `0.6.1`;
- upstream and Docker health.

## Rollout order

1. merge and validate the `1.0.0` development line;
2. build and scan an immutable candidate;
3. retain and hash the CycloneDX SBOM;
4. perform an isolated candidate acceptance;
5. complete Hermes/RITMO single-slot acceptance;
6. record explicit production approval;
7. run preflight;
8. run dry-run;
9. execute controlled deployment;
10. observe a defined stability window before enabling any optional capability.

Retry, circuit breaker, metrics and tracing are independent post-base gates and
must never be enabled together with the initial `1.0.0` rollout.
