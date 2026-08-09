# Phase 3 Mutation Semantics — `create_branch` and `create_pr`

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> These are proposed typed contracts. No tool of this name exists in the
> canonical registry, and none may be registered before
> `DIRECT_READ_ACCEPTED`.

## Shared execution ordering (fail-closed)

```text
typed mutation request
        ↓
tool classification (MUTATION, security tier, idempotency class)
        ↓
exact repository scope            → DENY without credential resolution
        ↓
policy evaluation (per operation) → DENY / APPROVAL_REQUIRED / ALLOW
        ↓
approval verification (operation_digest, single-use, unexpired)
        ↓
idempotency lookup                → return prior result, do not re-execute
        ↓
preconditions re-read (TOCTOU window closes here)
        ↓
write capability readiness → authorization material
        ↓
HTTPS write to api.github.com (no redirects, trust_env=false)
        ↓
audit/evidence record finalized
        ↓
shaped, redacted result
```

Every stage failure is a stable redacted reason code. Unknown state is DENY.

## `github.create_branch`

**Semantics:** create a *new* ref only. It is never an update.

| Field | Type | Rules |
|---|---|---|
| `repository` | `owner/repo` | Must be in the exact allow-list |
| `branch` | ref name | Strict grammar; rejected if it exists, starts with `-`, contains `..`, `//`, `\`, whitespace, control or non-ASCII characters, or resolves outside `refs/heads/` |
| `base_sha` | 40-hex commit SHA | Required; caller must pin the base explicitly |
| `idempotency_key` | opaque | Optional; derived if absent (see idempotency doc) |

- REST: `POST /repos/{owner}/{repo}/git/refs` with `ref=refs/heads/{branch}`.
- **No force.** `PATCH .../git/refs/{ref}` is not exposed in Phase 3.
- Base is a SHA, never a symbolic ref: this removes the "base moved" TOCTOU.
- GitHub returns `422` when the ref exists; that is mapped to a stable
  `REF_ALREADY_EXISTS` and is treated as *success* only when the existing ref
  points at exactly `base_sha` **and** a matching idempotency record exists.
- Idempotency class: `IDEMPOTENT_BY_PRECONDITION`.
- Compensation: delete the created ref, allowed only when the ref still points
  at `base_sha` and no PR references it.

## `github.create_pr`

**Semantics:** open one pull request from an existing head branch.

| Field | Type | Rules |
|---|---|---|
| `repository` | `owner/repo` | Exact allow-list |
| `head` | branch name | Must exist; must not equal `base` |
| `base` | branch name | Must be in the policy-declared allowed base set for the repository |
| `title` | string | Bounded length; no control characters |
| `body` | string | Bounded length; treated as data, never templated into anything executable |
| `draft` | bool | Default `true` for the first accepted iteration |
| `expected_head_sha` | 40-hex | Required; the PR is rejected if `head` no longer points here |

- REST: `POST /repos/{owner}/{repo}/pulls`.
- **Never auto-merges**, never sets auto-merge, never requests reviewers or
  assigns teams in Phase 3.
- Duplicate open PR for the same `head`→`base` pair is detected before the call;
  the existing PR is returned as an idempotent hit, not re-created.
- Maintainer-can-modify and cross-fork PRs are out of scope: `head` must be a
  branch in the same repository.
- Idempotency class: `IDEMPOTENT_BY_PRECONDITION`.
- Compensation: close the PR (never delete); the branch is compensated
  separately and only if it was created in the same operation chain.

## Result shaping

Both operations return an explicit field allow-list (repository, ref/number,
SHA, URL, state, digest, idempotency status) under a canonical byte budget, in
the same style as the Phase 2 read results. Raw provider payloads are never
forwarded.
