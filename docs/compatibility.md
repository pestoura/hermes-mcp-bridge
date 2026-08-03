# Compatibility

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
