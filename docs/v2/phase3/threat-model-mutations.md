# Phase 3 Threat Model — GitHub Mutations

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

Layered on `../security/threat-model.md`. Only threats that change
character when the operation *writes* are enumerated here.

## Additional assets

Repository history and refs; default-branch integrity; branch protection state;
PR review/approval state; merge commits; CI trust (a merged commit can trigger
privileged workflows); write-capable credential material; idempotency and
approval records.

## Mutation-specific threats

| ID | Threat | Primary control |
|---|---|---|
| T3-01 | Duplicate mutation from client retry, transport retry or resumed run | Idempotency key + pre-flight existence probe (ADR-0022) |
| T3-02 | Approval replay: an approval for operation A reused for operation B | `operation_digest` binding, single-use, expiry, nonce (ADR-0021) |
| T3-03 | TOCTOU: base ref, PR head or check state changes between approval and execution | Expected-SHA preconditions, re-validation immediately before the write |
| T3-04 | Confused deputy: caller induces a write on an out-of-scope repository | Exact repository allow-list evaluated before policy/credential resolution |
| T3-05 | Privilege escalation via merge into a protected/default branch | Default-branch merge DENY; governed merge only with explicit policy target |
| T3-06 | Supply-chain escalation: merged content executes privileged CI | Merge requires required-checks-green precondition and human approval |
| T3-07 | Ref smuggling: crafted branch names (`refs/`, `..`, unicode, `-`-prefixed) | Strict ref grammar validation, no shell interpolation |
| T3-08 | Prompt/tool injection causing an unintended write | Deterministic typed input only; no free-text-to-operation translation in DIRECT |
| T3-09 | Overprivileged write credential usable for admin/delete | Separate capability, `Administration` permission never granted (ADR-0020) |
| T3-10 | Destructive operation reached by policy default | Fail-closed default DENY for every unlisted mutation; `github.delete_repo` permanently DENY |
| T3-11 | Force-push / history rewrite through an allowed primitive | No force flag exposed; `create_branch` cannot update an existing ref |
| T3-12 | Partial failure leaves an orphan branch or a stale open PR | Declared compensation per operation; unsafe compensation → manual intervention |
| T3-13 | Idempotency store poisoning or key collision across principals/repos | Keys scoped by principal + repository + operation + digest; store is server-side only |
| T3-14 | Audit gap: a write is executed without a retained evidence record | Write-ahead intent record before the provider call; missing record fails the gate |
| T3-15 | Rate-limit exhaustion turning into repeated write attempts | `NO_RETRY` default for non-idempotent writes; `Retry-After` honored, no blind retry |
| T3-16 | Secret leakage via mutation error bodies (GitHub echoes request context) | Same redacted stable error classes as Phase 2; provider bodies never passed through |

## Trust assumptions that must be re-validated for Phase 3

- The write credential's *actual* granted permissions, verified by probe, not by
  configuration intent.
- Branch protection state of the target repository is read at execution time and
  is not cached across approvals.
- The Phase 2 read path's scope enforcement remains the single source of
  repository authorization; Phase 3 must not add a second, weaker path.
