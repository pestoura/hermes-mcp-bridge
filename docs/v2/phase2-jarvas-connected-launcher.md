# Phase 2 Jarvas connected acceptance launcher

> **Status:** credential provisioning completed by the operator; repository-side
> mint/rotation helper is integrated and GREEN; `DIRECT_READ_ACCEPTED` remains
> deliberately undeclared until the real connected collector and validator pass.

The canonical Jarvas-side launcher is:

```text
scripts/v2_phase2_connected_jarvas.sh
```

It exists to remove ad-hoc shell steps from the Phase 2 connected gate while
preserving the same fail-closed evidence contract.

## What it automates

From the actual Jarvas host the launcher:

1. verifies the private acceptance runtime and GitHub App private-key posture;
2. resolves the active Hermes `state.db` from `HERMES_HOME` or the canonical
   default Hermes home without printing the path;
3. creates a clean temporary checkout of the current accepted `main`;
4. rotates the short-lived GitHub App installation token with
   `v2_github_app_mint.py`;
5. regenerates the sanitized provider attestation from the verified mint
   response;
6. configures only the file-backed DIRECT secret provider;
7. builds the exact five-tool target topology for
   `pestoura/hermes-mcp-bridge`;
8. executes the connected collector for exactly three repetitions per tool;
9. runs `validate_v2_phase2_direct_read_evidence.py`;
10. prints only a sanitized aggregate gate summary.

No PEM, App JWT, installation token, authorization header, environment dump,
prompt text, raw provider output, V1 output or secret path is printed or retained
as acceptance evidence.

## V1 shadow non-mutation basis remains evidence, not a switch

The launcher intentionally defaults to:

```text
HERMES_V2_SHADOW_MUTATION_BASIS=none
```

and therefore fails closed with `SHADOW_MUTATION_BASIS_UNPROVEN` before any
connected sample unless one of the canonical bases has actually been
established for the run window:

```text
github_audit_log_reviewed
read_only_credential_enforced
```

Supplying either value is not, by itself, proof. The meaning remains identical
to the Phase 2 acceptance contract:

- `github_audit_log_reviewed` may be used only after applicable GitHub audit /
  security evidence for the collection window has actually been reviewed;
- `read_only_credential_enforced` may be used only if the GitHub credential
  effectively reachable by the V1 shadow during that window is constrained to
  read-only authority.

The current upstream Hermes Runs API does not expose a per-run toolset or
credential restriction field. The V1 bridge's `expected_actions` and
`resource_scopes` inputs are advisory/preflight metadata and are not a runtime
sandbox for the upstream agent. They must therefore not be repurposed as false
proof of non-mutation.

## Canonical targets

The launcher uses stable repository-scoped reads:

- repository metadata for `pestoura/hermes-mcp-bridge`;
- merged PR `#54`;
- issue `#51`;
- checks for the exact source commit under test;
- repository-scoped search for `DIRECT_READ_ACCEPTED`.

The collector expands these five intents to exactly 15 samples and compares the
full normalized DIRECT result shape with the V1 agentic shadow result.

## Promotion rule

The launcher may print `DIRECT_READ_ACCEPTED` only when the canonical validator
returns zero failures. Until that happens:

```text
PHASE2_CONNECTED_EVIDENCE_PENDING
DIRECT_READ_ACCEPTED_NOT_DECLARED
PHASE3_NOT_STARTED
```

This launcher does not weaken the prerequisite gate and is not CI/mock evidence.
It must be executed against the real Jarvas/Hermes runtime.
