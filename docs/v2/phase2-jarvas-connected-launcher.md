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
7. asks the installed Hermes resolver which toolsets it would enable, suppresses
   every result except MCP server `phase2-read`, and fails closed unless the
   second resolver pass returns exactly that one server;
8. starts that Hermes runtime with an empty inherited environment and only the
   model material required for inference;
9. probes the **live** `/health`, `/v1/capabilities`, `/v1/toolsets` and session
   database readiness endpoints, requiring zero enabled native/configurable
   toolsets;
10. re-runs the installed Hermes resolver against the persisted shadow home and
    verifies the exact one-server/five-tool MCP configuration;
11. starts a disposable instance of the unchanged 27-tool V1 Bridge pointed at
    that isolated Hermes runtime;
12. builds the exact five-tool target topology for
    `pestoura/hermes-mcp-bridge`;
13. executes the connected collector for exactly three repetitions per tool,
    using the isolated Hermes `state.db` for real V1 token accounting;
14. runs the original connected evidence validator and the companion live shadow
    isolation validator through `validate_v2_phase2_connected_gate.py`;
15. prints only a sanitized aggregate gate summary.

No PEM, App JWT, installation token, authorization header, environment dump,
prompt text, raw provider output, V1 output, Hermes log or secret path is printed
or retained as acceptance evidence.

## V1 shadow non-mutation is mechanically derived

The launcher does not accept `HERMES_V2_SHADOW_MUTATION_BASIS` from the operator.
The automated path always uses:

```text
read_only_credential_enforced
```

but only after the live shadow proof establishes all of the following:

- `/v1/toolsets` has **zero enabled native/configurable toolsets**;
- the installed Hermes `_get_platform_tools()` resolver returns exactly
  `phase2-read` for `api_server`;
- the persisted config contains exactly one MCP server, `phase2-read`;
- its native include list is exactly the five GitHub GET tools;
- resources and prompts are disabled;
- parallel MCP calls are disabled.

Current Hermes names those runtime MCP functions using
`mcp__<server>__<tool>`, producing exactly:

```text
mcp__phase2_read__github_get_checks
mcp__phase2_read__github_get_issue
mcp__phase2_read__github_get_pr
mcp__phase2_read__github_get_repo
mcp__phase2_read__github_search
```

The MCP server fixes the repository scope and delegates every provider operation
to the existing GET-only `GitHubDirectReadExecutor`. It has no shell, filesystem,
browser, code execution, messaging or mutation API. The subsequent 15 connected
samples supply positive evidence that the five allowed MCP functions are
actually callable through the V1 agentic path.

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
