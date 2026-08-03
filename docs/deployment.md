# Deployment

## Phase 1 — local loopback

1. Enable the Hermes API server on `127.0.0.1:8642` with a dedicated key.
2. Build and run the bridge on `127.0.0.1:8765`.
3. Validate `hermes_health`, connected `hermes_prompt`, session continuity, status, wait and cancellation.
4. Confirm MCP progress notifications, Hermes SSE events, heartbeat and final output in the original tool call.
5. Validate the automatic fallback from the Hermes event stream to run-status polling through unit tests.
6. Keep all operational testing read-only until the contract is proven.

Required local duration campaign:

- short connected run;
- five-minute connected run;
- client interruption with the Hermes run surviving by default;
- explicit `stop_on_disconnect=true` cancellation;
- detached `wait_seconds=0` regression.

## Phase 2 — Cloudflare Tunnel

1. Use a dedicated tunnel named `hermes-mcp` with ingress:
   - hostname `hermes-mcp-origin.hex0r.xyz` -> `http://127.0.0.1:8765`
   - HTTP Host Header override to `127.0.0.1:8765`
   - final fallback `http_status:404`
2. Keep the Hermes API on `127.0.0.1:8642` with no public route.
3. Protect the public MCP origin with Cloudflare Access Service Auth bound to a dedicated service token.
4. Preserve Streamable HTTP POST/GET/DELETE, MCP headers and SSE responses.
5. Disable caching, transformation and response buffering for `/mcp`.
6. Confirm that no inbound server port is opened.
7. Validate remote connected execution, detached recovery, stop, and evidence-backed duration limits through the tunnel.

### Deployed operational model

- Public endpoint: `https://hermes-mcp-origin.hex0r.xyz/mcp`
- Purpose: machine-to-machine operational access to the Hermes MCP bridge.
- Client requirement: callers must provide valid Cloudflare Access Service Auth headers.
- This endpoint is not the final ChatGPT custom-app endpoint.
- Access: Cloudflare Access Service Auth with one dedicated service token.
- Tunnel: dedicated `hermes-mcp` connector, isolated from other tunnels.
- Bridge listener: `127.0.0.1:8765` only.
- Hermes API listener: `127.0.0.1:8642` only.
- No inbound ports are opened. cloudflared establishes outbound-only connections to Cloudflare.
- No secrets in Git; service token material is stored outside the repository with restricted filesystem permissions.
- Cloudflare Access service token and Cloudflare Tunnel token are independent credentials with separate rotation paths.
- Cloudflare Access service token rotation: generate a new Client Secret, distribute it to MCP clients, then revoke the previous secret; definitive revocation is performed by deleting the service token. This does not require changing the tunnel token or cloudflared configuration.
- Cloudflare Tunnel token rotation: update the protected token file used by `cloudflared-hermes-mcp.service` and restart/control the service. This does not change the Cloudflare Access service token.
- Rollback: stop and disable `cloudflared-hermes-mcp.service`; confirm the endpoint no longer routes traffic; keep Access protection while the route/DNS still exist; remove the route and DNS only in final deactivation; revoke the Access service token separately; do not remove the Access policy while the tunnel remains active.

### Validated operational evidence

- Remote transport and authentication: `REMOTE_TRANSPORT_AND_AUTH_PASS`
- Remote connected execution completed in the original call with final result delivery.
- Observed remote session duration: `315.27s`
- Observed heartbeats in the original call: `21`
- Detached recovery validated with `wait_seconds=0`, `execution_id` returned, and recoverable completion.
- Stop validated for a detached read-only execution with terminal cancellation state observed.
- Formal campaign decision: `REMOTE_ORIGIN_SERVICE_TOKEN_PARTIAL`
- Supervisory conclusion: `REMOTE_360_SECOND_WORKLOAD_NOT_PROVEN`

A heartbeat reduces idle-timeout risk but does not override an absolute timeout imposed by Cloudflare or the MCP client. Remote duration claims require evidence from the real path.

## Phase 3 — ChatGPT custom app

1. Enable Developer Mode in an eligible ChatGPT workspace or account surface.
2. Create a custom MCP app pointing to `https://hermes-mcp.hex0r.xyz/mcp`.
3. Complete the configured authentication flow and scan tools.
4. Confirm the seven expected tools and the updated schemas.
5. Test read-only delegation first.
6. Validate one connected request at increasing durations without asking the user to poll manually.
7. Test recovery after a deliberately interrupted client connection.
8. Review write-capable behaviour only after authentication, confirmation and duration evidence are satisfactory.

## Promotion gates

| Gate | Required decision |
|---|---|
| Local v0.3 connected execution | `CONNECTED_LONG_RUN_LOCAL_PASS` |
| Cloudflare transport and authentication | `REMOTE_MCP_TRANSPORT_PASS` |
| ChatGPT discovery and connected execution | `CHATGPT_MCP_E2E_PASS` |

Do not merge or promote a phase solely because a shorter preceding test passed.
