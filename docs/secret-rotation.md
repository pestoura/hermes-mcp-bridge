# Secret rotation runbook

Canonical discovery, planning and rotation for bridge secrets.
Implementation: `src/hermes_mcp_bridge/secret_rotation.py`.

## Guarantees

- Secrets are **never** printed or logged. Comparison is by SHA-256 prefix,
  sanitized identity and length only.
- All paths used by a rotation plan are **absolute, canonical, and derived from
  the effectively discovered sources**. Relative paths and symlinks are rejected
  (`plan_rotation`/`apply_rotation` validate this). The plan transports the
  resolved paths, so `apply`/`rollback` cannot operate on unexpected files.
- `API_SERVER_KEY` effective source is discovered from the gateway
  systemd unit `Environment`/`EnvironmentFile`, the unit's `WorkingDirectory`
  `.env`, and `/proc/<pid>/environ` (digest comparison only). We do **not**
  assume `EnvironmentFile` contains the key.
- `HERMES_API_KEY` is discovered from `compose/.env` and the working-dir `.env`.
- Source status is one of `consistent`, `mismatch`, `insufficient`,
  `unknown`. A single present source is `insufficient` (gateway-memory vs
  files comparison requires >=2 present sources). Rotation verification only
  reports `verified` when status is `consistent`.
- Modes: `inspect`, `plan`, `apply`, `finalize`, `rollback`, `verify`.
- Rotation never restarts the gateway inside the agent process. It emits a
  short external script and a `systemd-run --user` **transient** unit whose
  name (<=55 chars) is separate from the real target service. The real service
  name is never truncated or altered.
- If active API runs exist, planning aborts by default unless `--force`.
- `docker restart` does **not** re-read `EnvironmentFile`/`.env` for the
  bridge; use `compose ... force-recreate` (or recreate the bridge container)
  when the bridge must re-read env. Only recreate the bridge when needed.

## File-backed secrets and HMAC rotation (0.9.0)

Every secret-bearing variable supports a `<NAME>_FILE` companion. **The file
wins over the environment value.** Values are read on demand and never cached,
so replacing the file content rotates the key without a restart.

| Purpose | Env | File variant |
| --- | --- | --- |
| Hermes API key | `HERMES_API_KEY` | `HERMES_API_KEY_FILE` |
| Current HMAC key | `HERMES_BRIDGE_HMAC_SECRET` | `HERMES_BRIDGE_HMAC_SECRET_FILE` |
| Previous HMAC key (verify-only) | `HERMES_BRIDGE_HMAC_SECRET_PREVIOUS` | `HERMES_BRIDGE_HMAC_SECRET_PREVIOUS_FILE` |

Non-secret identifiers `HERMES_BRIDGE_HMAC_KEY_ID` and
`HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID` are surfaced in health/readiness so an
operator can tell which key is active without ever seeing key material.

Minimum key length is `BRIDGE_MIN_SECRET_LENGTH` (default 32). Shorter keys are
rejected and make `security_posture` `not_ready`.

Rotation with grace period:

1. write the new key to the current secret file, and copy the outgoing key to
   the previous secret file;
2. set `HERMES_BRIDGE_HMAC_KEY_ID` / `HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID`;
3. new signatures use the current key only — the previous key is **verification
   only**, never used to sign;
4. once no in-flight manifest predates the swap, remove the previous key file
   and its key id. Verification of old signatures then correctly fails.

Compose mounts these as Docker secrets (`secrets:` block in `compose.yml`).
The `*_FILE` variables default to empty, so an env-only deployment is
unchanged. Keep secret files outside the repository, mode `0400`, owned by the
bridge UID. Health/readiness expose only `configured`, `required`,
`source_type` and `key_id` — never values, never full paths.

## Inspect (safe, read-only)

```
python - <<'PY'
from hermes_mcp_bridge.secret_rotation import inspect_secrets
import json
print(json.dumps(inspect_secrets(), indent=2))
PY
```

`comparable=True` means every present source agrees by digest and status is
`consistent`.

## Plan / apply / verify / rollback

- `plan_rotation(key, new_value=..., force=..., target_service=...)` -> dry
  plan with absolute discovered paths and a `plan_token` fingerprint of the
  intended change. Aborts if active runs or health unknown (fail-closed).
- `apply_rotation(plan)` re-validates active runs/health at apply time
  (reduces TOCTOU), checks the `plan_token` to reject manually built/tampered
  plans, writes atomically, preserves owner/mode, keeps `*.pre-rotation`
  backups at `0600`. Requires `dry_run=False`.
- `verify_rotation(key)` reports `verified`/`inconclusive` by digest only.
- `rollback_rotation(plan)` restores both sides from the operation's own
  `*.pre-rotation` backups (unique per operation) and returns an explicit
  external step for restart/verify. It does **not** declare operational
  success while the in-memory process is not yet reconciled.

## Window with active runs

If `active_api_runs > 0` (or health unknown), do NOT rotate. Wait for the
drain window or use `force=True` only with explicit operator authorization.

## docker restart vs compose force-recreate

- `docker restart <bridge>`: restarts the same container; env already baked
  in at creation is reused. Does NOT re-read changed `.env`/`EnvironmentFile`.
- `docker compose ... up -d --force-recreate <bridge>`: recreates the
  container, re-reading compose env/`.env`. Use this when the bridge must
  pick up a new secret.

## Pre-checklist

- [ ] `inspect_secrets()` shows `status=consistent` (or you accept mismatch).
- [ ] No active API runs, or `force=True` authorized.
- [ ] Plan paths are absolute/canonical and match discovered sources.
- [ ] External restart script/unit prepared (not executed inside agent).
- [ ] `*.pre-rotation` backup paths known.

## Post-checklist

- [ ] `verify_rotation(key)` confirms alignment by digest (no values shown).
- [ ] Bridge/gateway health returns 200.
- [ ] If env changed, bridge recreated (not merely restarted) when required.

## Decision

- `PASS`: rotated, verified by digest, health 200, no value exposed.
- `PARTIAL`: rotated but verify inconclusive; keep monitoring, do not expose.
- `FAIL_ROLLED_BACK`: rotation aborted or rolled back; both sides restored
  from `*.pre-rotation`, bridge recreated if needed.

## Incident regression coverage

- Wrong source `n8n.env`: effective gateway source empty -> `insufficient`.
- Empty value hash: empty value yields empty digest, `comparable=False`.
- `docker restart` not re-reading env: runbook mandates force-recreate.
- systemd unit name too long: transient unit name enforced <=55 chars and is
  separate from the real target service name.
- `set -e` in interactive shell: external script uses `set -euo pipefail`
  only in non-interactive context.
- Gateway in-memory value differs from files: `verify` classifies `mismatch`
  and reports `inconclusive`.
- Relative/symlink paths: rejected by plan/apply validation (CWD-independent).
- Manually built plan: rejected by `plan_token` check at apply time.
