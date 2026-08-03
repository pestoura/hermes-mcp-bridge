# Operations

## Capability manifest

Call `hermes_capabilities` to retrieve the canonical tool manifest and upstream capability state.

## Agent card

Call `hermes_agent_card` to retrieve the versioned bridge identity.

## Manifest stale detection

Clients should treat a change in `manifest_hash` returned by `hermes_health` as a signal to refresh cached tool schemas. The environment variable `MCP_TOOL_MANIFEST_STALE` is reserved for future client-side use; the bridge itself does not set it.

## Rollback

Rollback to 0.3.x by checking out the previous release tag. The 0.4.0 protocol fields are additive and optional; older clients ignore them.
