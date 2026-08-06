# Hermes MCP Bridge 1.0.0 — isolated runtime acceptance

## Purpose

This gate runs the already-built `1.0.0` candidate image in a disposable Docker
stack before Trivy and SBOM generation complete the CI job.

It validates the real runtime image without connecting to production Hermes,
RITMO, production state, production secrets or production networks.

## Isolation model

Each run creates uniquely named resources:

- one private Docker network;
- one mock Hermes container;
- one candidate bridge container;
- one state volume;
- one secrets volume;
- one host-loopback-only dynamic MCP port.

The harness never uses:

- host networking;
- the production container name;
- `/home/estourpm` paths;
- the Docker socket inside either container;
- production volumes;
- RITMO tools or data;
- a real API or HMAC credential.

All containers, volumes and the network are removed in `finally` cleanup.

## Mock upstream

`tests/isolated/mock_hermes.py` implements only:

```text
GET /health
GET /health/detailed
GET /v1/capabilities
```

Every POST, PUT, PATCH and DELETE is rejected. The mock logs only bounded JSON
metadata: event, method, finite path class and authorization presence. It never
logs headers or request bodies.

The acceptance fails if the mock observes any non-GET request or any mutation
rejection event.

## Candidate hardening

The candidate runs with:

- the image's non-root `bridge:bridge` user;
- read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges`;
- bounded tmpfs at `/tmp`;
- private bridge network;
- MCP published only to `127.0.0.1` on a temporary port;
- no Docker socket;
- isolated state and secrets volumes;
- production policy mounted read-only.

The harness inspects Docker runtime configuration and fails if any invariant is
missing.

## Security posture

The disposable API and HMAC values are written to an isolated volume by a
one-shot root setup container, changed to UID/GID `1000:1000` and mode `0400`.
The bridge receives only:

```text
HERMES_API_KEY_FILE=/run/secrets/hermes_api_key
HERMES_BRIDGE_HMAC_SECRET_FILE=/run/secrets/hermes_bridge_hmac_secret
```

Raw secret values are not present in the candidate container environment.
Readiness must prove:

- policy valid and file-backed;
- HMAC required, configured and file-backed;
- expected non-secret key ID;
- no previous HMAC verifier;
- API key configured;
- zero security components failing.

## Read-only MCP probe

The harness calls exactly:

```text
hermes_health
hermes_readiness
hermes_capabilities
hermes_agent_card
```

It does not call submit, prompt, stop, approvals, execution, checkpoint,
continuation, saga or lock mutation tools.

The probe requires:

- bridge, manifest and contract `1.0.0`;
- schema `0.6.1`;
- exactly 27 tools and no extras/missing tools;
- upstream healthy;
- no unsupported tools;
- readiness `ready`;
- capability source `upstream` from the mock;
- agent-card version `1.0.0`;
- metrics, tracing export, retry and circuit breaker disabled;
- mutation retry and mutation circuit posture false.

## Restart and state

After the first successful probe, the harness:

1. runs SQLite `quick_check` and `integrity_check` in read-only mode;
2. records migration version;
3. validates that all current bridge log lines are JSON objects;
4. executes exactly one `docker restart` of the candidate;
5. waits for Docker health to return to `healthy`;
6. requires `RestartCount` to increase by exactly one;
7. repeats the complete read-only MCP probe;
8. requires the pre/post posture summaries to match;
9. repeats SQLite integrity checks and requires identical results;
10. validates all accumulated logs again.

This is a candidate-runtime restart test, not the RITMO single-slot restart
acceptance required for production.

## Log safety

Every non-empty log line from the candidate and mock must parse as one JSON
object. The complete serialized log set is checked for:

- disposable API key;
- disposable HMAC key;
- `/run/secrets` paths;
- bearer authorization text.

No raw log content is emitted by the harness on success. Failure output is
bounded and redacts the disposable credentials.

## CI order

The `3.12` CI path runs:

1. runtime image build;
2. isolated runtime acceptance;
3. Trivy HIGH/CRITICAL blocking scan;
4. CycloneDX generation and structural validation;
5. SBOM upload/retention attempt.

A candidate that fails runtime acceptance is never treated as security-release
evidence, even if image scanning would otherwise pass.

## Decision

The required success marker is:

```text
HERMES_BRIDGE_1_0_0_ISOLATED_ACCEPTANCE_PASS
```

The result explicitly records:

```text
production_touched=false
ritmo_used=false
authorized_restarts=1
```

This decision authorizes neither production deployment nor feature activation.
The Hermes/RITMO single-slot gate remains mandatory and tracked separately.
