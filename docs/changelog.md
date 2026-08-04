# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.9.0 — Resilience and Concurrency (Block 3)

### Added

- Deterministic SQLite concurrency guarantees for registry, approvals, locks
  and migrations (WAL, `busy_timeout` set before any locking statement,
  `BEGIN IMMEDIATE` for every read-modify-write).
- `hermes_mcp_bridge.resilience`: injectable clocks, bounded exponential
  backoff with seedable jitter and `Retry-After` parsing, bounded SQLite retry,
  a closed/open/half-open circuit breaker, idempotent run-state tracking and
  post-crash recovery helpers.
- Deterministic, seedable fault-injection kit under `tests/faultkit/`
  (timeouts, resets, truncated/invalid/out-of-order/duplicated SSE,
  429/500/502/503, SQLite busy/`OperationalError`, simulated disk-full,
  cancellation during persistence). Test-only, never imported by the runtime.
- CI-safe load harness `scripts/load_harness.py` with optional
  `soak-30m`/`soak-60m`/`soak-2h` profiles, checkpoints, sanitized JSON report
  and PASS/FAIL exit criteria.
- Bounded resilience metrics: `bridge_sqlite_retries_total`,
  `bridge_circuit_transitions_total`, `bridge_circuit_rejections_total`,
  `bridge_duplicate_events_total`, `bridge_out_of_order_events_total`,
  `bridge_recovery_runs_total`, `bridge_backoff_sleep_seconds`.
- `docs/resilience-0.9.md`.

### Changed

- Bridge version and manifest version bumped to `0.9.0`.
  `hermes_readiness.version_added` stays `0.8.0`.
- SSE failures fall back to polling idempotently, honouring `Retry-After`;
  cancellation never reports success.

### Deprecated

- None in this release.

### Removed

- None in this release.

### Security

- New metric labels (`state`, `source`, `upstream`) have allow-listed value
  domains; unknown values fold into `other`, so cardinality stays finite.
- No prompts, outputs, secrets or identifiers are logged, exported or written
  to harness reports.

### Compatibility

- No MCP tool added, removed or renamed. `schema_version` stays `0.6.1`.
- All resilience features default to **off**; behaviour is identical to 0.8.0
  until explicitly enabled. See `docs/compatibility.md`.

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
