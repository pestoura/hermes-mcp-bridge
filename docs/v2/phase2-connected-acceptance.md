# Phase 2 — Connected GitHub DIRECT Acceptance

> **V2 · CANARY/COLLECTOR IMPLEMENTED · CONNECTED CREDENTIAL BLOCKED · `DIRECT_READ_ACCEPTED` NOT DECLARED**

This document defines the evidence that must be collected on the **actual
Jarvas/Hermes runtime** before the GitHub DIRECT read-only path may be promoted.
Repository CI and hermetic MockTransport tests are prerequisites, not substitutes
for this gate.

## Gate principle

```text
repo-side DIRECT core GREEN
        ↓
actual Jarvas discovery
        ↓
dedicated github.read provider verified
        ↓
explicit canary/feature wiring
        ↓
5 operations × 3 repetitions
        ↓
DIRECT execution + V1 agentic shadow read
        ↓
normalized semantic comparison
        ↓
zero Hermes upstream/LLM use on DIRECT
        ↓
fail-closed evidence validator
        ↓
DIRECT_READ_ACCEPTED
```

The gate is implemented by:

```text
scripts/v2_phase2_direct_read_acceptance.py          # connected collector
scripts/validate_v2_phase2_direct_read_evidence.py
```

The validator cannot generate or infer evidence: it only validates an externally
collected connected evidence document. The collector cannot fabricate one
either — it fails closed before emitting anything if the provider attestation,
the canary wiring, the 5×3 topology or the real V1 token accounting cannot be
proven on the live runtime.

## Repo-side runtime (implemented)

| Concern | Module | Default |
| --- | --- | --- |
| Secret-safe authorization adapter | `v2/github_secret_provider.py` | not configured |
| Non-secret readiness view | `v2/github_readiness.py` | fail-closed |
| DIRECT canary router | `v2/github_canary.py` | **disabled** |
| Sanitized provider attestation | `v2/github_attestation.py` | live probes + external declaration required |
| Connected collector harness | `scripts/v2_phase2_direct_read_acceptance.py` | manual, connected-only |

Properties enforced by `tests/test_v2_phase2_canary_runtime.py`:

- the canary is **OFF by default** and executes nothing when disabled;
- none of these modules is imported by `server.py`; the V1 surface stays at
  exactly **27 tools** and no `github.*` tool is added to it;
- authorization material is read only at the final boundary from a restricted
  `<NAME>_FILE` secret file (mode `0600`, no symlink, no group/other bits), is
  never cached, and never appears in `repr`/canonical/result/error/evidence;
- a classic/broad PAT — prefixed, unprefixed 40-hex, or revealed by an
  `x-oauth-scopes` response header — is rejected at three independent layers
  (provider, readiness, attestation);
- a DIRECT sample never falls back silently: `RouteDecision.path` is always an
  explicit `DIRECT` or `V1_FALLBACK`, and a failed DIRECT attempt is reported as
  a DIRECT failure with a stable error code, never re-run under the same label.

## Required runtime identity

Evidence must prove:

- Bridge `1.0.0`;
- schema `0.6.1`;
- V1 tool count remains exactly `27`;
- the actual Jarvas host was reached;
- the V1 path is healthy;
- the DIRECT feature/canary path is explicitly enabled;
- V1 semantics remain unchanged;
- the exact 40-hex commit carrying the DIRECT core is recorded.

## GitHub provider gate

The connected discovery must identify the actual Jarvas-side GitHub provider
without printing or retaining its secret.

Accepted provider types for this gate:

- GitHub App installation credential;
- fine-grained GitHub token.

A classic/broad PAT is not accepted as Phase 2 least-privilege evidence.

Required capability:

```text
github.read
```

Required exact read permissions:

```json
{
  "checks": "read",
  "issues": "read",
  "metadata": "read",
  "pull_requests": "read"
}
```

The evidence must also record:

- `authenticated = true`;
- `least_privilege = true`;
- `broad_pat = false`;
- no unexpected permissions;
- exact repository scopes, with no wildcards;
- GitHub base URL `https://api.github.com`;
- GitHub REST API version `2026-03-10`.

No credential value, secret path, environment dump or authorization header may
be retained.

## Required samples

Exactly three connected repetitions are required for each tool:

- `github.get_repo`;
- `github.get_pr`;
- `github.get_checks`;
- `github.get_issue`;
- `github.search`.

Total required samples: **15**.

Each sample must identify the exact repository and prove it belongs to the
provider's declared repository scope.

### DIRECT side

Every DIRECT sample must prove:

- success;
- positive latency;
- exactly **one GitHub provider API call**;
- `hermes_upstream_calls = 0`;
- Hermes LLM input/output/total tokens = exactly `0`;
- positive raw provider bytes;
- positive returned bytes;
- returned bytes do not exceed raw bytes;
- no mutation observed;
- no redirect followed.

This is the core proof that a deterministic read did not silently fall back to
Hermes/LLM execution.

### V1 shadow side

The same read intent must also run through the existing V1 agentic path, without
performing a mutation.

Every shadow sample must prove:

- success;
- positive latency;
- real Hermes token accounting, not an estimate;
- positive total token usage;
- a named real accounting source;
- no mutation observed.

The V1 path remains the comparison baseline only; the DIRECT execution must not
invoke it internally.

### Semantic comparison

Comparison is performed over the **full default shaped result** of the DIRECT
executor — not a narrower subset. The collector derives the compared field set
from the executor's own public
`GITHUB_DIRECT_DEFAULT_RESULT_FIELDS` mapping, so the two can never drift:

```text
github.get_repo    full_name, private, visibility, default_branch, archived,
                   html_url, updated_at
github.get_pr      number, title, state, draft, merged, user, head, base,
                   html_url, updated_at
github.get_issue   number, title, state, state_reason, user, labels, assignees,
                   comments, is_pull_request, html_url, updated_at
github.get_checks  total_count, check_runs
github.search      total_count, incomplete_results, items
```

Reducing `github.get_checks`/`github.search` to `total_count` is explicitly not
sufficient: two materially different result sets share a count. A changed check
run or a changed search item must — and does — break the digest.

Normalization is canonical and deterministic and preserves semantic structure:
nested objects stay objects and arrays of objects stay arrays of objects (never
stringified). Collections whose order is not semantic (`check_runs`, `items`,
`labels`, `assignees`) are canonically ordered by each item's own canonical JSON
text; the default shape contains no order-bearing array, and the
`ORDER_SIGNIFICANT_FIELDS` set is kept explicit and empty so introducing one is
a deliberate act. The identical rule is applied to both sides before SHA-256.

The shadow prompt asks for exactly this shape — same keys, same nesting, no
extra fields — describing nested structures (`head`/`base`, `check_runs`,
`items`) by shape only, never by value. Prompts are never retained.

Evidence must retain only digests and the boolean comparison result:

```text
semantic_match = true
direct_normalized_sha256 = <64 hex>
v1_normalized_sha256     = <same 64 hex>
```

Raw prompt/output text is not required and must not be retained by the
acceptance evidence.

## Window isolation

Each sample must declare:

```text
connected_jarvas = true
contaminated_window = false
```

`contaminated_window = false` is not accepted as a bare boolean. Each sample
must carry the derived `window_integrity` record proving
`direct_transport_dedicated`, `direct_call_delta_exact` and
`shadow_session_scoped_accounting` are all true and
`attribution_ambiguity` is false, with no extra fields.

The document must also carry the top-level `window_integrity_basis`:

```text
direct_mutation_basis = executor_http_method_restricted_to_get
shadow_mutation_basis = github_audit_log_reviewed | read_only_credential_enforced
```

`none` and `unknown` are rejected. Each sample's DIRECT `mutation_basis` must be
the executor basis, and the V1 shadow `mutation_basis` must match the declared
top-level shadow basis. V1 `token_usage_source` must be exactly the canonical
`hermes_state_db:session_model_usage`, with `token_usage_estimated = false`.

If unrelated activity makes provider-call, Hermes-call or token attribution
ambiguous, the sample is rejected and must be recollected. The validator does
not average away contaminated observations.

## Provider provenance required by the validator

`least_privilege = true` is likewise not accepted on its own. The evidence must
carry `attestation_notes` proving:

- `attestation_path_recorded = false` (no secret path is ever stored);
- an external `declaration` with schema
  `hermes-v2-phase2-provider-attestation/1`, `confirmation = true`, a
  `confirmation_source` permitted for the provider type
  (fine-grained token → `github_settings_ui`; GitHub App →
  `github_app_settings_ui` or `installation_token_mint_response`) and a
  timezone-aware ISO-8601 `confirmed_at`;
- `externally_confirmed` exactly `exact_permission_map` and
  `exact_repository_selection`;
- `machine_verified` covering authentication, repository metadata read, pull
  requests read, issues read and check runs read — plus
  `installation_repository_set` for a GitHub App;
- `permissions_source` coherent with the provider type and confirmation source;
- live `probes`: `auth_probe_status = 200`,
  `oauth_scopes_header_present = false`, `repository_probe_count` equal to the
  number of provider repository scopes, `repository_read_probes` for all and
  only those scopes with each metadata/pulls/issues/check-runs status `200` and
  non-negative integer counts, `fine_grained_self_enumeration_available = false`
  for a fine-grained token, and `installation_repository_count` equal to the
  scope count for a GitHub App.

No secret path or secret value is required, accepted or inspected.

The `canary` block must additionally prove `direct_feature_enabled = true`,
`canary_tool_ids` exactly the five DIRECT tools, `canary_repositories` exactly
the provider repository scopes, and `wildcard_scopes = false`.

## Aggregate requirements

For 15 accepted samples the aggregate must prove:

```text
sample_count                    = 15
successful_samples              = 15
semantic_matches                = 15
direct_provider_api_calls       = 15
direct_hermes_upstream_calls    = 0
direct_hermes_llm_tokens        = 0
v1_shadow_hermes_llm_tokens     > 0
mutations_observed              = 0
contaminated_windows            = 0
```

Each of the five tool IDs must have exactly 3 repetitions numbered 1–3.

## Privacy contract

The retained evidence must explicitly state:

```json
{
  "credential_values_stored": false,
  "environment_dump_stored": false,
  "outputs_stored": false,
  "prompts_stored": false,
  "secret_paths_stored": false
}
```

The validator also rejects evidence containing forbidden secret-bearing keys
such as raw token/authorization/password/private-key fields.

## Validator result

Connected evidence is validated with:

```bash
python scripts/validate_v2_phase2_direct_read_evidence.py \
  /path/to/phase2-connected-evidence.json \
  --json-out /path/to/phase2-connected-gate.json
```

Success is exactly:

```json
{
  "failures": [],
  "gate": "DIRECT_READ_ACCEPTED",
  "source_commit": "<40-hex-connected-source-sha>"
}
```

Any missing/invalid property returns `DIRECT_READ_BLOCKED` and a non-zero exit
code.

## Current blocker

**CANARY/COLLECTOR IMPLEMENTED · CONNECTED CREDENTIAL BLOCKED.**

The repo-side runtime, the collector and the fail-closed validator now exist and
are covered by hermetic tests. The connected gate remains **unsatisfied** for one
reason only: the sole GitHub credential currently available on the Jarvas host is
a **classic broad PAT**, which this contract explicitly refuses as least-privilege
evidence. The 15 connected samples must not be collected until a GitHub App
installation credential or a fine-grained least-privilege token exists.

Unblocking sequence:

1. provision a GitHub App installation token or a fine-grained token scoped to
   the exact target repositories with `checks/issues/metadata/pull_requests =
   read` and no write permission;
2. write it to a `0600` file and point `BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE`
   at it (a bare environment value is rejected by design; the file is opened with
   `O_NOFOLLOW` and validated with `fstat` on that same descriptor, so a symlink
   or an inode substitution is never followed);
2b. write the sanitized `--provider-attestation` document confirming the exact
   permission map and repository selection;
3. run the collector against the live runtime with the state DB for real V1
   token accounting;
4. run the validator on the produced document.

ChatGPT's own GitHub connector is a different trust/credential boundary and is
not valid evidence for this gate.

### Machine-verified vs externally confirmed

The evidence keeps the two apart deliberately; nothing in the second column is
claimed as an API observation.

| Fact | How it is established |
| --- | --- |
| the credential authenticates | machine-verified — `GET /rate_limit` |
| the material is not a classic PAT | machine-verified — absence of `x-oauth-scopes` |
| repository metadata is readable | machine-verified — `GET /repos/{owner}/{repo}` |
| pull requests are readable | machine-verified — `GET /repos/{o}/{r}/pulls` |
| issues are readable | machine-verified — `GET /repos/{o}/{r}/issues` |
| check runs are readable | machine-verified — `GET /repos/{o}/{r}/commits/{default_branch}/check-runs` |
| the App installation repository set | machine-verified — `GET /installation/repositories` |
| the exact permission map | **externally confirmed** — `--provider-attestation` |
| the exact selected repository scope (fine-grained) | **externally confirmed**, plus runtime enforcement by `GitHubRepositoryScope` |

#### Why the permission map cannot be machine-verified

GitHub REST API `2026-03-10` has **no self-introspection endpoint for an
already-issued credential**:

- there is no `GET /installation/token/permissions`; that endpoint does not
  exist and is never called. An installation token's permissions/repositories
  appear in the *mint response* of
  `POST /app/installations/{installation_id}/access_tokens`, which the holder of
  an already-issued token cannot replay;
- `GET /installation/repositories` *is* valid for an installation token and is
  used to enumerate the installation's repository set;
- a **fine-grained PAT** can enumerate neither its own permission map nor its own
  selected repositories. GitHub additionally grants read access to *public*
  repositories independently of the selection, so a successful public-repo read
  is not evidence about the selection.

#### Why `GET /repos/{owner}/{repo}` `permissions` is not used

That block reports the **principal's computed role on the repository**, not the
capability of the token in hand: for a repository owner it reports
`admin: true` even when the fine-grained PAT is restricted to read. It is
therefore used neither to accept nor to reject a provider, and no
`REPOSITORY_WRITE_ACCESS_PRESENT` rejection exists. Write is never probed:
absence of write is established by the attestation plus the read-only executor,
never by attempting a mutation.

### The `--provider-attestation` input

The collector requires a sanitized, secret-free JSON document
(`hermes-v2-phase2-provider-attestation/1`):

```json
{
  "schema": "hermes-v2-phase2-provider-attestation/1",
  "provider_type": "fine_grained_token",
  "permissions": {
    "checks": "read",
    "issues": "read",
    "metadata": "read",
    "pull_requests": "read"
  },
  "unexpected_permissions": [],
  "repository_scopes": ["pestoura/hermes-mcp-bridge"],
  "confirmation": true,
  "confirmation_source": "github_settings_ui",
  "confirmed_at": "2026-08-08T10:00:00+00:00"
}
```

`confirmation_source` is restricted per provider type:

- `fine_grained_token` → `github_settings_ui`;
- `github_app` → `github_app_settings_ui` or `installation_token_mint_response`.

Any mismatch against the CLI `--provider-type`, the target repositories or the
exact permission map, any wildcard, any unexpected permission, and a missing or
false `confirmation` all fail closed **before** any evidence is produced. The
document's path is never recorded in the evidence, and secret-like keys in it are
rejected. This declaration does **not** replace the live probes: it is the
explicit external proof of what the GitHub API cannot self-introspect, instead of
the collector inventing the declaration.

The document is **schema-closed**: the eight keys above are the only accepted
top-level fields. Any other field — `credential_value`, `raw_token`, `notes`,
or any other creatively named extra — is rejected with the stable code
`ATTESTATION_UNEXPECTED_FIELD` *before* any content is processed, so the
sanitized input cannot be used to carry arbitrary or secret-like data.
Obviously secret-like names keep the more specific
`ATTESTATION_INPUT_SECRET_LIKE_FIELD`.

`confirmed_at` must be a **timezone-aware** ISO-8601 timestamp (explicit offset
or `Z`). A naive timestamp is rejected with
`ATTESTATION_CONFIRMED_AT_NOT_TIMEZONE_AWARE`, and an unparseable value with
`ATTESTATION_CONFIRMED_AT_INVALID`. No maximum age window is enforced at this
phase; the requirement exists for auditability and reproducibility.

### Token material is opaque

Authorization material is never parsed or length-validated. The provider
classifies it **only by prefix**: `github_pat_` (fine-grained), `ghs_` (GitHub
App installation), `ghp_`/`gho_` and unprefixed 40-hex (classic, always
rejected).

This matters for the stateless `ghs_<app-id>_<jwt>` installation-token format
GitHub has been rolling out during 2026: those tokens are variable-length,
routinely exceed 520 characters and contain dots, dashes and underscores. They
are accepted unchanged. The provider keeps only a generous defensive resource
bound (8192 bytes) on how much it reads from the secret file, plus a small
truncation floor — **neither is a GitHub format or length validation** and
neither may be tightened into one. Classic PAT rejection is unaffected. No real
token sample is reproduced in this repository or in any evidence.

### Window integrity and mutation claims

Neither `contaminated_window` nor `mutation_observed` is a hardcoded literal.

- `contaminated_window` is derived by the collector's window-integrity object
  from the isolation actually used: a dedicated transport per DIRECT sample, an
  exact one-call provider delta per sample, and `session_id`-scoped token
  accounting for the V1 shadow. With those three there is no attribution
  ambiguity between the two sides; if any fails, the collector aborts with
  `WINDOW_INTEGRITY_UNPROVEN` rather than claiming a clean window.
- DIRECT `mutation_observed` is derived from the executor's own structure: it
  exposes only the five reads and routes every request through a single GET
  helper, so no non-GET request can be emitted (`mutation_basis =
  executor_http_method_restricted_to_get`).
- V1 shadow `mutation_observed` has no robust runtime proof, so it requires an
  explicit `--shadow-mutation-basis`. The default `none` fails closed with
  `SHADOW_MUTATION_BASIS_UNPROVEN`; the accepted bases
  (`github_audit_log_reviewed`, `read_only_credential_enforced`) are recorded in
  the evidence alongside the claim.

### Local test matrix note

The local full-suite run was executed on Python 3.13.5, which is outside the
supported matrix. The 11 Phase 1 failures observed there reproduce on
`origin/main` and are therefore not a regression from this branch; they are not
addressed here. Python 3.11/3.12 is not available on this host, so the supported
matrix is confirmed by GitHub Actions after the PR.
