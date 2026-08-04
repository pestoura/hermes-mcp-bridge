# Compatibility

## 0.8.0 → 0.9.0

### Client compatibility

- No MCP tool was added, removed or renamed; all 27 tools of 0.8.0 keep their
  request and response contract.
- `schema_version` stays `0.6.1`: no migration is introduced, so a 0.9.0 bridge
  and a 0.8.0 bridge can read the same state database.
- `bridge_version` and `manifest_version` move to `0.9.0` (and therefore the
  manifest hash changes). `hermes_readiness.version_added` deliberately stays
  `0.8.0`.
- All resilience behaviour (retry, circuit breaker) is **disabled by default**,
  so the observed request pattern is byte-for-byte the 0.8.0 pattern until an
  operator opts in.

### Upgrade path

1. Deploy 0.9.0 with default configuration (all resilience flags off).
2. Verify `hermes_health` returns `bridge_version: "0.9.0"` and
   `schema_version: "0.6.1"`.
3. Re-cache the capability manifest: the hash changed with the version bump.
4. Enable `BRIDGE_RETRY_ENABLED` and/or `BRIDGE_CIRCUIT_ENABLED` only after
   confirming the new metrics are scraped.

### Rollback

Downgrading to 0.8.0 requires no schema action; the state database is
unchanged. See `docs/resilience-0.9.md`.

## 0.3.0 → 0.4.0

### Client compatibility

- Existing 0.3.0 clients can continue to call the 7 original tools without changes.
- New tools `hermes_capabilities` and `hermes_agent_card` are additive.
- Tool outputs gain an optional `envelope` field; old clients ignore unknown fields.
- `hermes_health` gains additional bridge discovery fields without removing existing ones.

### Upgrade path

1. Deploy `0.4.0` alongside `0.3.0` behavior.
2. Verify `hermes_health` returns `schema_version: "0.4.0"`, `manifest_version`, and `manifest_hash`.
3. Confirm old tool outputs still contain `session_id`, `execution_id`, `status`, `output`, `error`, and `metadata`.
4. Update clients to consume `hermes_capabilities` and `hermes_agent_card` when ready.
5. Cache manifests and monitor for `manifest_hash_divergence` in health responses.

### Deprecation policy

- Bridge contract versions are deprecated only by explicit changelog entry.
- Old fields remain present for at least one minor release after deprecation.
- New optional fields never remove or rename existing fields in the same release.
