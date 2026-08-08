# GitHub DIRECT Read-Only MVP

> **V2 · PHASE 2 CORE IMPLEMENTED · NOT YET ACCEPTED · NO V1 WIRING**

Phase 2 introduces the first deterministic provider path that bypasses the
Hermes LLM for known read-only work. The repo-side core is intentionally
isolated from the V1 MCP surface until connected Jarvas discovery and shadow
acceptance are complete.

## Execution path

```text
Typed GitHub operation
        ↓
canonical Tool Registry / direct-read classification
        ↓
exact repository scope
        ↓
fail-closed PolicyEngine + capability/github.read readiness
        ↓
GitHubAuthorizationProvider
        ↓
HTTPS GET → api.github.com
        ↓
endpoint-specific normalization
        ↓
explicit result shaping + byte budget
        ↓
GitHubDirectResult
```

There is no Hermes client, prompt, agent or LLM in this path. Repository scope
is checked before policy/readiness so an out-of-scope request cannot learn the
internal state of `github.read` and cannot trigger authorization resolution.

## Typed tools

| Tool | REST operation | Phase 2 scope |
| --- | --- | --- |
| `github.get_repo` | `GET /repos/{owner}/{repo}` | exact allowed repository |
| `github.get_pr` | `GET /repos/{owner}/{repo}/pulls/{number}` | exact allowed repository |
| `github.get_checks` | `GET /repos/{owner}/{repo}/commits/{ref}/check-runs` | exact allowed repository/ref |
| `github.get_issue` | `GET /repos/{owner}/{repo}/issues/{number}` | exact allowed repository |
| `github.search` | `GET /search/issues` | repository-constrained issue/PR search only |

`github.search` is deliberately narrower than GitHub's global search surface.
The caller provides plain search text plus structured `item_type`/`state`
filters; the executor constructs the GitHub qualifiers and always injects the
exact `repo:owner/repo` scope itself. Caller-supplied search qualifiers,
parentheses and boolean operators are rejected so a query cannot escape the
resource boundary. Code/repository/global-user search is outside this MVP.

## GitHub REST contract

The Phase 2 core pins:

```text
base URL: https://api.github.com
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2026-03-10
follow redirects: false
method: GET only
```

The code builds endpoint paths from validated owner/repository/ref/number
fields; no caller-controlled absolute URL exists. Environment proxy settings
are not inherited by the executor (`trust_env=false`).

Current GitHub documentation used for the MVP requires/readies these
fine-grained repository permissions:

- repository metadata: `Metadata: read`;
- pull requests: `Pull requests: read` (GitHub also documents Contents read as
  an alternative for the get-PR endpoint, but the V2 capability should prefer
  the domain-specific permission);
- check runs: `Checks: read`;
- issues: `Issues: read`;
- repository-scoped issue/PR search must only expose repositories visible to
  the credential; the planned provider therefore needs the relevant Issues and
  Pull Requests read permissions for the authorized repository set.

The final Jarvas credential shape is **not** decided by this core. ADR-0007
continues to prefer a GitHub App or fine-grained token over a broad PAT, with
separate read/write/admin capabilities.

## Credential boundaries

Phase 1's `CredentialBroker` remains status-only. It answers whether
`github.read` is `READY` without returning secrets.

Phase 2 adds a second, execution-boundary protocol:

```text
GitHubAuthorizationProvider.resolve("github.read", "owner/repo")
    -> GitHubAuthorization | None
```

`GitHubAuthorization` is deliberately non-canonical, has no public raw-value
property and redacts `str`/`repr`. The raw bearer material is exposed only by a
method used to build the final HTTP Authorization header. The only provider
implementation in the repository is an in-memory static test provider; it is
**not** a production secret backend.

A DIRECT call is denied before authorization material is resolved when:

- the tool is unknown/not DIRECT/not read-only;
- the repository is outside the exact allow-list;
- policy is not `ALLOW`;
- required capability/credential readiness is not `READY`.

The repository-scope check happens before the readiness broker is consulted.
Hermetic tests assert that an out-of-scope request performs zero readiness
lookups, zero authorization resolutions and zero HTTP requests.

If all gates pass but material cannot be resolved, execution still fails closed
with `CREDENTIAL_MATERIAL_UNAVAILABLE`.

## Result shaping

Raw provider payloads are not passed through. Each endpoint has a normalized
allow-list. The caller may request a subset of those fields; unknown fields are
rejected.

Large/prose-heavy fields such as PR/issue `body` are opt-in and search never
returns bodies. The shaped JSON is canonically serialized and checked against a
bounded result-byte budget before it is returned. Evidence records
`raw_bytes`/`returned_bytes` so later Phase 2 benchmarking can quantify context
reduction.

## Error model

Provider response bodies and request headers never enter errors. Stable error
codes cover:

- `AUTHENTICATION_FAILED`;
- `FORBIDDEN`;
- `RATE_LIMITED` (+ bounded numeric `Retry-After` when present);
- `NOT_FOUND`;
- `GONE`;
- `INVALID_REQUEST`;
- `REDIRECT_BLOCKED`;
- `UPSTREAM_ERROR` / `UPSTREAM_TRANSPORT_ERROR`;
- invalid JSON/shape/result budget.

A 403 with zero remaining rate-limit quota is classified as `RATE_LIMITED`.
Automatic redirects are disabled, preventing a redirect from forwarding the
Authorization header to another host.

## Acceptance status

The repository-side implementation and hermetic tests are **not** sufficient
for `DIRECT_READ_ACCEPTED`.

Still required:

1. fresh discovery on the actual Jarvas host of existing GitHub tooling,
   credential sources, scopes and repository access — without printing secret
   values;
2. provision/identify a dedicated least-privilege `github.read` capability;
3. provider health/authentication probe against an authorized test/private
   repository as appropriate;
4. wire the V2 DIRECT surface under an explicit feature/canary gate without
   changing V1 semantics;
5. run shadow comparisons for the five read operations against V1 agentic
   results, never duplicating mutations;
6. record latency, provider API-call count, raw-vs-returned bytes and prove
   **zero Hermes LLM token usage** on the DIRECT path;
7. retain fail-closed acceptance evidence and only then promote
   `DIRECT_READ_ACCEPTED`.
