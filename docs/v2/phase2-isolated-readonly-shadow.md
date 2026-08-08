# Phase 2 — mechanically proven read-only V1 shadow

> **Status:** repository implementation in progress. This document strengthens
> the Phase 2 promotion rule; it does not declare `DIRECT_READ_ACCEPTED`.

## Problem

The connected Phase 2 collector compares each deterministic DIRECT GitHub read
with the same intent through the V1 `hermes_prompt` agentic path. The original
contract correctly required a real basis for `mutation_observed = false`, but a
basis name such as `read_only_credential_enforced` must never become a bypass
string.

The current Hermes Runs API does not expose a per-run tool/credential sandbox.
Bridge `expected_actions` and `resource_scopes` are advisory/preflight metadata,
not a runtime capability boundary. They therefore cannot prove that the V1
agent was unable to mutate GitHub.

## Accepted automated solution

The canonical automated acceptance launcher now builds a **disposable isolated
Hermes shadow runtime**. It preserves the V1 agentic comparison itself — the
collector still calls the unchanged 27-tool Bridge `hermes_prompt` — while
constraining the Hermes instance behind that bridge to one exact runtime-probed
toolset:

```text
ChatGPT acceptance launcher
        |
        +--> DIRECT executor -----------------------> GitHub GET
        |
        +--> disposable V1 Bridge (27-tool contract)
                  |
                  +--> isolated Hermes api_server
                           |
                           +--> mcp-phase2-read
                                    |
                                    +--> github_get_repo
                                    +--> github_get_pr
                                    +--> github_get_issue
                                    +--> github_get_checks
                                    +--> github_search
                                             |
                                             +--> existing GET-only
                                                  GitHubDirectReadExecutor
```

The five MCP tools do not accept an owner/repository argument. The repository is
fixed by the private runtime configuration and protected again by
`GitHubRepositoryScope`. They reuse the same existing GitHub App file-backed
provider and GET-only DIRECT executor that Phase 2 is testing.

## Isolation boundary

The launcher creates a clean Hermes home outside the live Hermes home and starts
both the shadow Hermes gateway and shadow V1 Bridge with an empty inherited
environment (`env -i`). Only the minimum values required for the disposable
runtime are supplied.

The shadow home contains:

- the active model/provider configuration required for inference;
- only the provider credential material required for that model;
- a new loopback API-server bearer key;
- `platform_toolsets.api_server = [mcp-phase2-read]`;
- exactly one MCP server, `phase2-read`;
- `tools.include` containing exactly the five read tools;
- MCP resources disabled;
- MCP prompts disabled;
- parallel MCP tool calls disabled;
- a dedicated state database used for real per-session token accounting.

It deliberately does **not** copy messaging or integration credentials, model
fallback chains, or the live Hermes working state.

The disposable runtime is removed after the collection window. The live Hermes
home is never deleted or modified by the launcher.

## Live proof, not config inspection

Configuration alone is insufficient. Before any of the 15 connected samples,
`scripts/v2_phase2_probe_shadow_runtime.py` probes the running isolated Hermes
API and requires HTTP 200 from:

```text
/health
/v1/capabilities
/v1/toolsets
/api/sessions?limit=1
```

The `/v1/toolsets` response must report **exactly one enabled toolset**:

```text
mcp-phase2-read
```

and exactly these effective Hermes tools:

```text
mcp_phase2_read_github_get_checks
mcp_phase2_read_github_get_issue
mcp_phase2_read_github_get_pr
mcp_phase2_read_github_get_repo
mcp_phase2_read_github_search
```

Any extra toolset/tool, missing tool, failed probe, non-loopback API, missing API
authentication, repository drift or source-commit drift blocks collection.

The sessions probe also initializes the isolated Hermes session database before
the collector begins strict read-only `session_model_usage` accounting.

## Shadow isolation evidence

The live probe writes only sanitized evidence with schema:

```text
hermes-v2-phase2-shadow-isolation/1
```

It records the source commit, exact repository scopes, exact effective tools,
GET-only server contract, probe status codes and a timezone-aware confirmation
time. It does not record the API key, GitHub token, PEM, provider credential
value, prompts, raw outputs or secret paths.

## Promotion authority

The original validator remains mandatory:

```text
scripts/validate_v2_phase2_direct_read_evidence.py
```

It still validates the entire 15-sample connected evidence contract.

However, the canonical **promotion authority** is now the stricter wrapper:

```text
scripts/validate_v2_phase2_connected_gate.py
```

That strict gate requires both:

1. zero failures from the original connected evidence validator; and
2. a valid live shadow-isolation document bound to the same source commit and
   exact provider repository scopes.

For the automated canonical launcher, the V1 shadow mutation basis must be:

```text
read_only_credential_enforced
```

and it is accepted only together with the mechanical isolation proof.

The legacy `github_audit_log_reviewed` basis remains meaningful as historical or
manual evidence under the original contract, but it is **not** sufficient for
the automated strict promotion gate.

## Fail-closed order

```text
GitHub App mint/attestation
        -> prepare disposable Hermes home
        -> start isolated Hermes
        -> probe exact live tool surface
        -> start disposable unchanged V1 Bridge
        -> collect 5 tools x 3 repetitions
        -> original connected validator
        -> shadow-isolation validator
        -> strict combined promotion gate
        -> DIRECT_READ_ACCEPTED
```

Any failed step returns `DIRECT_READ_BLOCKED` or an equivalent stable shadow
isolation failure and Phase 3 remains blocked.
