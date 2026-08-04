# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
  shapes, 27 tools, readiness present, schema 0.6.1, missing/extra detection).

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
