# Production rollout — 0.9.0

The controlled assets for this release live in `deploy/0.9.0/`. The frozen
`deploy/0.8.2/` bundle is not modified.

0.9.0 keeps the client-visible surface stable:

- 27 MCP tools;
- wire/database schema `0.6.1`;
- `bridge_version` and `manifest_version` `0.9.0`;
- `hermes_readiness.version_added` remains `0.8.0`.

The release integrates base-image hardening, policy/HMAC fail-closed security,
single-stream observability and deterministic SQLite/concurrency resilience.
Retry, circuit breaker, tracing and metrics remain disabled during the base
rollout.

## Promotion state

A merge to `main` is not a production promotion. Before executing this bundle,
all of the following evidence is mandatory:

1. CI green on the exact integrated SHA in Python 3.11 and 3.12.
2. Runtime image built from that exact SHA with OCI labels:
   `org.opencontainers.image.version=0.9.0` and
   `org.opencontainers.image.revision=<full-sha>`.
3. Blocking Trivy scan with no fixed HIGH/CRITICAL finding.
4. CycloneDX SBOM generated, structurally validated **and retained** outside the
   ephemeral runner.
5. Exact known-good 0.8.2 rollback image tag and immutable image ID recorded.
6. Production container currently healthy.
7. File-backed candidate secrets and production policy pass the read-only
   preflight, and the running production container matches the exact rollback
   image ID.
8. An isolated Hermes/RITMO lifecycle campaign on one slot passes before wider
   dispatcher use.

At the time this bundle was prepared, SBOM generation and validation were green
but GitHub artifact retention was blocked by the account storage quota.
Therefore deployment remains **NO-GO** until retention is proven after quota
cleanup or through an approved alternative repository.

## Files

| Path | Role |
| --- | --- |
| `deploy/0.9.0/lib.sh` | fixed project, immutable image assertions, secret/policy checks, bounded health polling |
| `deploy/0.9.0/preflight.sh` | read-only GO/NO-GO |
| `deploy/0.9.0/deploy.sh` | SQLite backup and candidate rollout; dry-run by default |
| `deploy/0.9.0/validate.sh` | read-only MCP health, contract and security-posture validation |
| `deploy/0.9.0/rollback.sh` | exact-image rollback to 0.8.2; dry-run by default |
| `deploy/0.9.0/compose.candidate.yml` | 0.9.0 candidate with policy and file-backed secrets |
| `deploy/0.9.0/compose.rollback.yml` | known-good 0.8.2 service definition |

## Safety contract

- Every Compose invocation uses `-p hermes-mcp-bridge`.
- Mutation requires both `EXECUTE_DEPLOYMENT=YES` and
  `EXPECTED_SHA=<REQUIRED_SHA>`.
- `REQUIRED_SHA` is mandatory even for dry-run, so the plan always refers to a
  concrete candidate.
- Candidate image revision and version labels must match the release; the
  running container image ID must match the locally inspected immutable
  candidate ID.
- Rollback requires an explicit tag and exact immutable Docker image ID, and the
  running container must match that ID after rollback.
- SQLite is backed up through the Python `sqlite3` backup API before recreation.
- Rollback does not alter SQLite because schema `0.6.1` is unchanged.
- The candidate mounts only the current Hermes API key and HMAC key. Previous
  HMAC verification is optional and is not part of the first production step.
- No secret value, environment-file contents or container `.Config.Env` is
  printed.
- Metrics, tracing, retry and circuit breaker must remain off for the initial
  deployment. They are enabled only in separate, evidence-backed phases.
- No port is published. MCP and Hermes API stay on loopback/host networking.

## Transitional rollback requirement

0.9.0 consumes `HERMES_API_KEY` from
`/run/secrets/hermes_api_key`. The current rollback baseline, 0.8.2, predates
the file-backed credential contract. During this rollout only:

- keep a non-empty `HERMES_API_KEY` in the protected deployment env file for
  rollback compatibility;
- also provide the same candidate credential through the restricted secret
  file;
- candidate Compose explicitly overrides the raw API/HMAC/policy-inline
  variables to empty values and forces the file-backed paths, so no raw secret
  from the rollback env file remains in the candidate runtime environment;
- do not remove the env fallback until the known-good rollback baseline itself
  supports secret files.

This is deliberate temporary dual provisioning, not the target steady state.

## Host preparation

Default paths can be overridden through the documented environment variables.

```bash
install -d -m 0700 -o 1000 -g 1000 \
  /home/estourpm/hermes-mcp-bridge/secrets

install -m 0600 -o 1000 -g 1000 /dev/null \
  /home/estourpm/hermes-mcp-bridge/secrets/hermes_api_key

install -m 0600 -o 1000 -g 1000 /dev/null \
  /home/estourpm/hermes-mcp-bridge/secrets/hermes_bridge_hmac_secret
```

Populate those files through the operator secret store or an interactive method
that does not place values in shell history. The HMAC secret must contain at
least 32 characters.

The protected env file must contain, without exposing values in logs:

```text
HERMES_API_KEY=<temporary rollback-compatible value>
HERMES_BRIDGE_HMAC_KEY_ID=<non-secret rotation identifier>
BRIDGE_SECURITY_MODE=production
```

The candidate overrides the security mode, policy path and secret-file paths
fail-closed. The policy file must be available at:

```text
/home/estourpm/hermes-mcp-bridge/config/policies/production.json
```

## Build candidate

Run from a clean checkout of the exact integrated SHA:

```bash
git status --short
git rev-parse HEAD

docker build \
  --tag hermes-mcp-bridge:0.9.0-<shortsha>-candidate \
  --label org.opencontainers.image.version=0.9.0 \
  --label org.opencontainers.image.revision=<full-sha> \
  --label org.opencontainers.image.source=https://github.com/pestoura/hermes-mcp-bridge \
  .
```

Record without retagging:

```bash
docker image inspect hermes-mcp-bridge:0.9.0-<shortsha>-candidate \
  --format '{{.Id}}'
```

## Preflight and dry-run

```bash
EXPECTED_SHA_0_9_0=<full-sha> \
CANDIDATE_IMAGE=hermes-mcp-bridge:0.9.0-<shortsha>-candidate \
ROLLBACK_IMAGE=<exact-known-good-0.8.2-tag> \
ROLLBACK_IMAGE_ID=<sha256:exact-id> \
./deploy/0.9.0/preflight.sh
```

Expected terminal decision:

```text
PREFLIGHT_0_9_0: GO
```

Then print the immutable plan without mutation:

```bash
REQUIRED_SHA=<full-sha> \
CANDIDATE_IMAGE=hermes-mcp-bridge:0.9.0-<shortsha>-candidate \
ROLLBACK_IMAGE=<exact-known-good-0.8.2-tag> \
ROLLBACK_IMAGE_ID=<sha256:exact-id> \
./deploy/0.9.0/deploy.sh
```

Expected:

```text
DEPLOY_0_9_0: DRY_RUN OK
```

## Execute candidate rollout

Only after every promotion gate, including SBOM retention and the approved
maintenance window:

```bash
EXECUTE_DEPLOYMENT=YES \
EXPECTED_SHA=<full-sha> \
REQUIRED_SHA=<full-sha> \
CANDIDATE_IMAGE=hermes-mcp-bridge:0.9.0-<shortsha>-candidate \
ROLLBACK_IMAGE=<exact-known-good-0.8.2-tag> \
ROLLBACK_IMAGE_ID=<sha256:exact-id> \
./deploy/0.9.0/deploy.sh
```

The script:

1. repeats the full preflight;
2. creates a restricted SQLite backup;
3. compares immutable image IDs and force-recreates the fixed Compose service
   only when the exact candidate ID is not already running;
4. waits using the healthcheck-derived bounded budget;
5. checks initialize and the 27-tool contract;
6. checks `hermes_health`;
7. requires `hermes_readiness.status=ready`;
8. requires file-loaded policy, configured file-backed HMAC, non-empty key ID,
   valid policy hash and no failing security component.

## Isolated Hermes/RITMO acceptance

Do not enable recurring dispatchers as part of the container replacement.
Validate one controlled slot first:

1. confirm bridge readiness and zero unexpected restarts;
2. claim at most one eligible read-only test run in the selected RITMO slot;
3. start and complete it through the 0.9.0 bridge;
4. prove no duplicate submission after SSE/polling convergence;
5. restart the candidate once and prove recovery does not resubmit;
6. verify SQLite integrity and absence of stuck lease/run state;
7. confirm logs remain one redacted JSON object per line;
8. leave retry, circuit breaker, metrics and tracing disabled.

Required decision:

```text
HERMES_BRIDGE_0_9_0_SINGLE_SLOT_ACCEPTANCE_PASS
```

Without that decision, production stays on 0.8.2 or is rolled back.

## Independent validation

```bash
REQUIRE_0_9_SECURITY=1 ./deploy/0.9.0/validate.sh
```

This is read-only: it creates no run, approval, plan, checkpoint, lock or saga.

## Rollback

Rollback requires the same release SHA gate, plus the exact known-good image ID:

```bash
EXECUTE_DEPLOYMENT=YES \
EXPECTED_SHA=<full-sha> \
REQUIRED_SHA=<full-sha> \
ROLLBACK_IMAGE=<exact-known-good-0.8.2-tag> \
ROLLBACK_IMAGE_ID=<sha256:exact-id> \
./deploy/0.9.0/rollback.sh
```

The rollback service uses the protected env file and the unchanged SQLite
schema. It does not mount the 0.9.0 policy or HMAC secret. Validation checks the
0.8.2 contract, schema, upstream and overall readiness, but does not require the
0.9.0 security-posture fields.

## Post-acceptance phases

After a stable observation window, enable features separately:

1. metrics exporter and existing Prometheus integration according to
   `observability-rollout-0.9.0.md`;
2. bounded retry in a controlled canary;
3. circuit breaker only after retry behaviour and thresholds are observed;
4. tracing only when an approved OpenTelemetry endpoint exists.

Each phase has an independent rollback. Do not combine these changes with the
base image promotion.
