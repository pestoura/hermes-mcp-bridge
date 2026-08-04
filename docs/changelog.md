# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
