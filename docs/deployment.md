# Deployment phases

## Phase 1 — local loopback

1. Enable the Hermes API server on `127.0.0.1:8642` with a dedicated key.
2. Build and run the bridge on `127.0.0.1:8765`.
3. Validate `hermes_health`, connected `hermes_prompt`, session continuity, status and cancellation.
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

1. Add `hermes-mcp.hex0r.xyz` to the existing Cloudflare Tunnel configuration.
2. Point only to `http://127.0.0.1:8765`.
3. Keep the Hermes API on `127.0.0.1:8642` with no public route.
4. Protect the MCP endpoint with an authentication method supported by the intended ChatGPT custom-app flow.
5. Preserve Streamable HTTP POST/GET/DELETE, MCP headers and SSE responses.
6. Disable caching, transformation and response buffering for `/mcp`.
7. Confirm that no inbound server port is opened.
8. Validate durations through the tunnel at 5, 15, 30 and 60 minutes.

A heartbeat reduces idle-timeout risk but does not override an absolute timeout imposed by Cloudflare or the MCP client. Remote duration claims require evidence from the real path.

## Phase 3 — ChatGPT custom app

1. Enable Developer Mode in an eligible ChatGPT workspace or account surface.
2. Create a custom MCP app pointing to `https://hermes-mcp.hex0r.xyz/mcp`.
3. Complete the configured authentication flow and scan tools.
4. Confirm the four expected tools and the updated `hermes_prompt` schema.
5. Test read-only delegation first.
6. Validate one connected request at increasing durations without asking the user to poll manually.
7. Test recovery after a deliberately interrupted client connection.
8. Review write-capable behaviour only after authentication, confirmation and duration evidence are satisfactory.

## Promotion gates

| Gate | Required decision |
|---|---|
| Local v0.2 connected execution | `CONNECTED_LONG_RUN_LOCAL_PASS` |
| Cloudflare transport and authentication | `REMOTE_MCP_TRANSPORT_PASS` |
| ChatGPT discovery and connected execution | `CHATGPT_MCP_E2E_PASS` |

Do not merge or promote a phase solely because a shorter preceding test passed.
