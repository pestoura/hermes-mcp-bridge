# Phase 2 Jarvas connected acceptance launcher

> **Status:** credential provisioning completed by the operator; repository-side
> mint/rotation helper is integrated and GREEN; `DIRECT_READ_ACCEPTED` remains
> deliberately undeclared until the real connected collector and strict combined
> promotion gate pass.

The canonical Jarvas-side launcher is:

```text
scripts/v2_phase2_connected_jarvas.sh
```

It removes ad-hoc shell steps from the Phase 2 connected gate while preserving a
fail-closed evidence boundary.

## Source integrity

The connected run is bound to the **exact Git commit of the checkout containing
the launcher**. The launcher resolves its repository root from its own script
location, records the exact 40-hex `HEAD`, rejects a locally modified launcher,
and then creates a separate clean checkout detached at that same commit. All
mint, shadow, collector and validator code used by the run comes from the pinned
checkout.

This prevents a moving `main` ref from changing the code under test between
review/CI and connected Jarvas execution. The `source_commit` recorded in the
connected evidence is therefore the same commit that supplied the reviewed
launcher.

## What it automates

From the actual Jarvas host the launcher:

1. binds the run to the exact clean launcher checkout commit;
2. verifies the private acceptance runtime and GitHub App private-key posture;
3. creates a clean temporary checkout detached at that exact commit;
4. rotates the short-lived GitHub App installation token with
   `v2_github_app_mint.py`;
5. regenerates sanitized provider attestation from the verified mint response;
6. creates a disposable isolated Hermes home without modifying the live Hermes
   home;
7. starts that Hermes runtime with an empty inherited environment and only the
   model material required for inference;
8. exposes exactly one dynamic API-server toolset, `mcp-phase2-read`, backed by
   five fixed-repository GitHub GET tools;
9. probes the **live** `/health`, `/v1/capabilities`, `/v1/toolsets` and session
   database readiness endpoints and fails closed on any unexpected tool/capability;
10. starts a disposable instance of the unchanged 27-tool V1 Bridge pointed at
    that isolated Hermes runtime;
11. builds the exact five-tool target topology for
    `pestoura/hermes-mcp-bridge`;
12. executes the connected collector for exactly three repetitions per tool,
    using the isolated Hermes `state.db` for real V1 token accounting;
13. runs the original connected evidence validator and the companion live shadow
    isolation validator through `validate_v2_phase2_connected_gate.py`;
14. prints only a sanitized aggregate gate summary.

No PEM, App JWT, installation token, authorization header, environment dump,
prompt text, raw provider output, V1 output or secret path is printed or retained
as acceptance evidence.

## V1 shadow non-mutation is mechanically derived

The launcher no longer accepts `HERMES_V2_SHADOW_MUTATION_BASIS` from the
operator. The automated path always uses:

```text
read_only_credential_enforced
```

but only **after** the live isolated Hermes probe has proved that the effective
API-server tool surface is exactly:

```text
mcp_phase2_read_github_get_checks
mcp_phase2_read_github_get_issue
mcp_phase2_read_github_get_pr
mcp_phase2_read_github_get_repo
mcp_phase2_read_github_search
```

The MCP server fixes the repository scope and delegates every provider operation
to the existing GET-only `GitHubDirectReadExecutor`. It has no shell, filesystem,
browser, code execution, messaging or mutation API.

The current upstream Hermes Runs API does not expose a per-run toolset or
credential restriction field. The V1 Bridge's `expected_actions` and
`resource_scopes` remain advisory/preflight metadata; the launcher does not
misrepresent them as a runtime sandbox.

Full rationale and evidence contract:

```text
docs/v2/phase2-isolated-readonly-shadow.md
```

## Canonical targets

The launcher uses stable repository-scoped reads:

- repository metadata for `pestoura/hermes-mcp-bridge`;
- merged PR `#54`;
- issue `#51`;
- checks for the exact pinned source commit under test;
- repository-scoped search for `DIRECT_READ_ACCEPTED`.

The collector expands these five intents to exactly 15 samples and compares the
full normalized DIRECT result shape with the V1 agentic shadow result.

## Promotion rule

The original validator:

```text
scripts/validate_v2_phase2_direct_read_evidence.py
```

remains mandatory but is no longer the sole automated promotion authority. The
canonical launcher promotes only through:

```text
scripts/validate_v2_phase2_connected_gate.py
```

which requires both the complete connected evidence contract and the live
shadow-isolation proof bound to the same source commit and repository scopes.

Until that strict gate returns zero failures:

```text
PHASE2_CONNECTED_EVIDENCE_PENDING
DIRECT_READ_ACCEPTED_NOT_DECLARED
PHASE3_NOT_STARTED
```

This launcher is not CI/mock evidence. It must execute against the real
Jarvas/Hermes runtime.
