# Phase 3 Least-Privilege Credential Split

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

Refines ADR-0007 for the mutation case. No credential is provisioned, changed or
inspected by this document.

## Capability separation

| Capability ID | Phase | Purpose | Fine-grained permissions (intended) |
|---|---|---|---|
| `github.read` | 2 | The accepted read-only DIRECT path | `Metadata: read`, `Pull requests: read`, `Checks: read`, `Issues: read` |
| `github.write.branch` | 3 | Create a new branch ref only | `Contents: write` (with `Metadata: read`) |
| `github.write.pr` | 3 | Open a pull request | `Pull requests: write` (with `Metadata: read`, `Contents: read`) |
| `github.write.merge` | 3 | Governed conditional merge | `Pull requests: write`, `Contents: write`, `Checks: read` |
| `github.admin` | — | **Never provisioned in V2** | `Administration` must never be granted |

## Rules

1. **No shared credential.** A read operation must never be executable with a
   write capability and vice versa. Capability resolution is per typed tool.
2. **No classic PAT, no broad PAT.** Phase 2 already rejects classic/broad PATs;
   Phase 3 inherits that rejection unchanged.
3. **Repository-scoped installation.** The write capability is installed only on
   the exact repository allow-list, never organization-wide.
4. **Separate readiness state.** Each write capability has its own seven-state
   readiness entry. A healthy `github.read` never implies a ready
   `github.write.*`.
5. **Probe-verified permissions.** Before `DIRECT_MUTATION_ACCEPTED`, the actual
   granted permission set is read from the installation and compared against the
   intended set above. A superset is a **failure**, not a convenience.
6. **No admin surface.** Repository deletion, permission changes, webhook
   management, protection changes and org operations are out of V2 scope and
   must be unrepresentable — not merely denied by policy.
7. **Rotation.** Write capabilities use short-lived installation tokens with the
   same mint/rotation boundary described in
   `../github-app-runtime-credential.md`; no long-lived write token is
   stored.
8. **Redaction.** The broker returns status only. Write credential material must
   never appear in results, logs, traces, metric labels or evidence files.

## Open items carried forward

- OD-016 (GitHub App vs fine-grained token) stays open, but Phase 3 assumes the
  Phase 2 GitHub App boundary is the default; a divergence needs its own ADR.
- Whether `github.write.merge` should be a distinct installation from
  `github.write.pr` is deferred to the ADR-0020 decision record.
