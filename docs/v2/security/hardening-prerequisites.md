# Hardening Prerequisites

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

These findings were observed during the audit and are prerequisites/risk items, not changes made by this baseline.

1. **Broad GitHub PAT** — move v2 toward GitHub App/fine-grained least privilege before enabling DIRECT mutations.
2. **`SUDO_PASSWORD` observed in environment** — remove/segment/review before expanding direct privileged execution.
3. **Backups of env/config with cleartext secrets** — perform secret hygiene and reduce secret copies.
4. **Policy posture** — `unknown_action = DENY` is positive, but audited `deny_actions` and `require_approval_actions` were not populated; introduce granular typed-tool policy before mutations.
5. **Google token health** — an access token was observed expired while refresh/keepalive exists; introduce credential readiness and do not equate configured with healthy.
6. **Broad terminal/filesystem surface** — v2 Direct Executor must not project unrestricted `$HOME`/shell access.

No secret values are stored in this documentation.

## Promotion preconditions

DIRECT read-only may only be promoted after registry/policy/credential readiness controls exist. DIRECT mutations additionally require least-privilege credentials, idempotency/replay protection, approval binding, locks where needed, mutation-specific tests and rollback/compensation semantics.
