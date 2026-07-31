# Deployment phases

## Phase 1 — local loopback

1. Enable Hermes API server on `127.0.0.1:8642` with a dedicated key.
2. Build and run the bridge on `127.0.0.1:8765`.
3. Validate `hermes_health`, `hermes_prompt`, continuation and cancellation with MCP Inspector.
4. Keep all testing read-only until the contract is proven.

## Phase 2 — remote tunnel

1. Add `hermes-mcp.hex0r.xyz` to the existing Cloudflare Tunnel config.
2. Protect it with an OAuth-compatible access policy.
3. Confirm Streamable HTTP POST/GET/DELETE and required MCP headers traverse the proxy.
4. Validate timeout behaviour for long runs.

## Phase 3 — ChatGPT app

1. Enable Developer Mode in an eligible ChatGPT workspace.
2. Create a custom app pointing to `https://hermes-mcp.hex0r.xyz/mcp`.
3. Complete OAuth and scan tools.
4. Test read-only delegation first.
5. Review and approve write-capable behaviour only after evidence is satisfactory.
