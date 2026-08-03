# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
