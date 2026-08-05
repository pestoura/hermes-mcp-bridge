# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.9.0 — Observability operational hardening (BLOCO 6C, phase 1)

No tool-contract change: still 27 tools, wire schema `0.6.1`, no SQLite
migration. Metrics remain off by default and no port is published.

### Added

- **Single-stream log hygiene** (`observability/quiet.py`). Third-party loggers
  (`httpx`, `httpcore`, `uvicorn*`, `mcp`, `starlette`, `anyio`, `urllib3`, …)
  lose their own handlers and are re-emitted through the bridge's redacting
  JSON formatter, so every line on the stream parses as JSON and library
  messages are redacted like bridge events. `warnings.warn()` is captured into
  logging instead of raw stderr. Configurable via
  `BRIDGE_LOG_CAPTURE_THIRD_PARTY` (default on) and
  `BRIDGE_LOG_THIRD_PARTY_LEVEL` (default `WARNING`). Applying the policy is
  idempotent and never raises.
- **Exporter bind-scope classification**: `bind_scope()` now returns
  `loopback`, `docker-gateway` (`172.17.0.1`, `host.docker.internal`) or
  `remote`, and `exporter_status()` reports `remote_exposure_allowed`.
- `deploy/observability/`: Prometheus scrape snippet (single job, bearer token
  via `credentials_file`), alerting rules with `summary`+`runbook` annotations
  and allow-listed low-cardinality labels only, an Alertmanager example with a
  loopback receiver, and a README with the security preconditions. These are
  snippets for an existing stack — they start no second Prometheus/Alertmanager
  and publish no port.
- `scripts/observability_smoke.py`: offline validation of the deploy assets and
  of the log pipeline (valid JSON, no duplicates, no secrets), plus an optional
  authenticated `--probe` of a running exporter. Tokens are read from the
  environment or a file and never echoed.
- `docs/observability-rollout-0.9.0.md`: rollout order, verification matrix,
  rollback and known limitations.
- `tests/test_observability_block6c_0_9_0.py`: 46 directed tests covering log
  hygiene, bind scope and unchanged exporter authorization, deploy-asset
  parsing/credential/cardinality checks, compose rotation, the tracing shim and
  the smoke-script contract.

### Changed

- `compose.yml` caps container logs at `json-file`, `max-size=10m`,
  `max-file=5` (~50 MiB per service) so a chatty dependency cannot exhaust the
  host disk, and passes the two new log-hygiene variables through.
- Bridge log propagation stays **enabled** (embedders and `pytest`'s `caplog`
  keep seeing records); duplicates are prevented by a `BridgeTreeFilter` on the
  bridge-installed root handler rather than by disabling propagation.

### Deprecated

- `hermes_mcp_bridge.tracing` is now a thin re-export of
  `hermes_mcp_bridge.observability.tracing` and emits a `DeprecationWarning` on
  import (captured into the JSON log stream). It will be removed in a future
  major release. The canonical `parse_traceparent` is stricter than the old root
  implementation — it additionally rejects version `ff`, an all-zero trace id
  and an all-zero span id. This is a fail-closed tightening; valid traceparents
  parse identically.

## 0.9.0 — Base image security (Python 3.12 slim Trixie, digest-pinned)

### Changed

- **Container base image** moves from the floating tag
  `python:3.11-slim-bookworm` to
  `python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`,
  pinned by digest and referenced from a single `ARG BASE_IMAGE` so the builder
  and runtime stages cannot drift apart. Scanned with identical Trivy flags and
  **without** `--ignore-unfixed`, this takes the image from 6 CRITICAL / 20 HIGH
  (0.8.2 baseline) to 4 CRITICAL / 19 HIGH.
- Package, bridge and manifest version bumped to `0.9.0`. The wire schema stays
  `0.6.1`, the tool set stays at 27 with `hermes_readiness` mandatory, and there
  is no new SQLite migration. Clients caching the capability manifest must
  refresh because `bridge_version`/`manifest_version`/`manifest_hash` move.

### Security

- Runtime stage now installs `ca-certificates` and runs `update-ca-certificates`
  explicitly, so outbound TLS validation does not depend on base-image defaults.
- `apt-get clean` added alongside `--no-install-recommends` and the apt-list
  cleanup in every installing layer; `__pycache__` and `/root/.cache` are removed
  from the final layer.
- systemd, dbus and init-system-helpers remain deliberately absent. The three
  known failing tests on 3.11/3.12/3.13 alike are host-only `systemctl` /
  `systemd-run` cases from the secret-rotation flow; they are reported in the
  normal host gates rather than papered over by installing systemd in the image.

### Added

- `docs/base-image-security-0.9.0.md`: the decision, the CVE matrix, the test
  matrix across 3.11/3.12/3.13, and the rejection rationale for
  3.11-slim-bookworm, 3.13-slim-trixie, Alpine and distroless.
- `tests/test_base_image_0_9_0.py`: static contract tests requiring a
  digest-pinned Python 3.12 Trixie base shared by both stages, no floating
  `python:` reference anywhere, non-root `bridge:bridge` as the last transition
  before `CMD`, minimal/cleaned apt usage, CA certificates present, no build
  toolchain in the runtime stage, the SQLite state directory owned by the bridge
  user, compose privilege-drop preserved, and the 27-tool / `0.6.1` contract
  unchanged.

## 0.8.2 — Health-settle correctness (false-rollback fix)

### Fixed

- **False rollback during rollout.** `deploy/0.8.1/deploy.sh` waited a fixed
  `SETTLE_SECONDS=12` after `docker compose up -d` and then ran `validate.sh`.
  The container healthcheck declares `start_period=10s`, `interval=30s`,
  `timeout=5s`, `retries=3`, so the first probe is only recorded at ~10s and the
  Docker health status is still `starting` at 12s. Validation therefore raced
  the healthcheck and reported failure on a perfectly healthy container. The
  0.8.1 production rollout only completed after a manual `SETTLE_SECONDS=60`
  override — a magic number, not a fix.

### Added

- `health_settle_budget()` in `deploy/0.8.2/lib.sh` derives the stabilisation
  budget from the container's own healthcheck configuration:
  `start_period + (interval + timeout) * retries + HEALTH_SETTLE_MARGIN_SECONDS`,
  clamped to `[HEALTH_SETTLE_MIN_SECONDS, HEALTH_SETTLE_MAX_SECONDS]`
  (defaults 30s and 300s). For the production healthcheck this yields 130s.
- `wait_for_health()` replaces the fixed sleep with a bounded poll of the Docker
  health status (`HEALTH_POLL_INTERVAL_SECONDS`, default 2s). Health criteria:
  `healthy` => pass; `unhealthy` => fail immediately; `starting` inside the
  budget => keep waiting (NOT a failure); budget expiry => fail. A container
  with no declared healthcheck warns and passes.
- `HEALTH_FALLBACK_*` knobs cover containers that declare no healthcheck fields.
- `HEALTH_REQUIRE_HEALTHCHECK=1` makes `wait_for_health()` fail closed when a
  container declares no healthcheck. `deploy.sh` and `rollback.sh` set it: a
  container that cannot prove readiness must not count as a successful
  deployment. Read-only callers keep the permissive warn-and-pass behaviour.
- `tests/test_health_settle_0_8_2.py`: reproduces the defect exactly (12s of
  `starting` then `healthy` before 60s must PASS), plus `unhealthy` => FAIL,
  permanent `starting` => timeout FAIL, budget derivation, floor/ceiling
  clamping, fallback path, and a guard that health logs never expose container
  `Env`.

### Changed

- Package, bridge and manifest version bumped to `0.8.2`. Schema stays `0.6.1`
  and the tool set stays at 27 with `hermes_readiness` mandatory. No new SQLite
  migration.
- Rollout tooling moved to `deploy/0.8.2/`. `deploy.sh`, `rollback.sh` and
  `validate.sh` all use the same `wait_for_health()` logic, so a rollback can no
  longer be declared failed while its container is legitimately starting.
- `validate.sh` waits for health before probing the MCP surface.
- Rollback defaults now target `hermes-mcp-bridge:rollback-0.8.1-a3c8c11`.
- Version assertions in `tests/test_observability_health.py` and
  `tests/test_contracts_0_8_2.py` derive from `contracts.CURRENT_CONTRACT_VERSION`
  instead of hard-coded literals, so future bumps no longer break them.
- Rollout script tests scrub ambient `EXECUTE_DEPLOYMENT`/`ROLLBACK_IMAGE_ID`
  style variables from the environment, making them hermetic on an operator
  shell that has a previous rollout exported.

### Operational note

Operators must no longer pass `SETTLE_SECONDS`. The budget is derived. Use
`HEALTH_SETTLE_SECONDS` only to override deliberately, and
`HEALTH_SETTLE_MARGIN_SECONDS` / `HEALTH_SETTLE_MIN_SECONDS` /
`HEALTH_SETTLE_MAX_SECONDS` to tune policy.

## 0.8.1 — Rollout contract hardening

### Added

- `hermes_mcp_bridge.contracts`: single source of truth for the required tool
  set per contract version, with `required_tools`, `expected_tool_count`,
  `diff_tools` and `validate_tools` (missing/extra detection).
- `hermes_readiness` now reports a `tool_contract` component (contract version,
  schema version, count, expected count, missing, extra) and the response
  carries `contract_version` and `schema_version`.
- Versioned rollout tooling under `deploy/0.8.1/`: `lib.sh`, `preflight.sh`,
  `deploy.sh`, `rollback.sh`, `validate.sh`, `compose.candidate.yml`,
  `compose.rollback.yml`.
- Rollout/rollback runbook `docs/production-rollout-0.8.1.md`.
- Tests: `tests/test_contracts_0_8_1.py` and `tests/test_rollout_scripts_0_8_1.py`
  (fixed Compose project, non-mutating dry-run, candidate/rollback command
  shapes, 27 tools, readiness present, schema 0.6.1, missing/extra detection,
  `bash -n` parsing and ShellCheck when available).

### Changed

- Package, bridge and manifest version bumped to `0.8.1`. Schema stays `0.6.1`.
- Every Docker Compose invocation in the rollout scripts is pinned with
  `-p hermes-mcp-bridge` via a single `compose()` helper, so the project name
  can no longer be derived from the current working directory.
- `deploy.sh` and `rollback.sh` are dry-run by default; mutation requires both
  `EXECUTE_DEPLOYMENT=YES` and a matching `EXPECTED_SHA`, and both are
  idempotent when the container already runs the target image.
- `scripts/smoke_test.py` and `protocol._MANIFEST_TOOL_NAMES` derive the tool
  set from `contracts` instead of hard-coded lists.

### Removed

- Blind 26-tool assumptions in rollout/validation paths. 26 remains declared
  only as the historical 0.6.x contract in `contracts.TOOL_CONTRACTS`.

### Security

- Scripts print no secrets and never echo `.env` contents; rollback performs no
  SQLite mutation (schema 0.6.1 is unchanged across 0.8.x).

### Compatibility

See `docs/compatibility.md`.

## 0.4.0 — Protocol Foundations

### Added

- Versioned execution envelope with `schema_version` and optional provenance fields.
- Formal message/event types with typed Pydantic models and tolerant upstream parser.
- Capability negotiation: canonical `CapabilityManifest`, `ToolManifest`, and `hermes_capabilities` tool.
- Agent card: versioned `AgentCard` and `hermes_agent_card` tool.
- Health discovery fields: `manifest_version`, `manifest_hash`, `bridge_version`, `schema_version`, and divergence flag.

### Changed

- Bridge version bumped to `0.4.0`.
- `hermes_health` now includes manifest metadata without breaking existing shape.

### Deprecated

- None in this release.

### Removed

- None in this release.

### Security

- No prompts, tool outputs, secrets, or credentials are stored in manifests or cards.

### Compatibility

See `docs/compatibility.md`.
