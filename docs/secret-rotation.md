# Secret rotation runbook

Canonical discovery, planning and rotation for bridge secrets.
Implementation: `src/hermes_mcp_bridge/secret_rotation.py`.

## Guarantees

- Secrets are **never** printed or logged. Comparison is by SHA-256 prefix,
  sanitized identity and length only.
- `API_SERVER_KEY` effective source is discovered from the gateway
  systemd unit `Environment`/`EnvironmentFile`, the unit's `WorkingDirectory`
  `.env`, and `/proc/<pid>/environ` (digest comparison only). We do **not**
  assume `EnvironmentFile` contains the key.
- `HERMES_API_KEY` is discovered from `compose/.env` and the working-dir `.env`.
- Modes: `inspect`, `plan`, `apply`, `finalize`, `rollback`, `verify`.
- Rotation never restarts the gateway inside the agent process. It emits a
  short external script and a `systemd-run --user` unit with a name <=55
  chars, absolute path, timeout, and sanitized evidence.
- If active API runs exist, planning aborts by default unless `--force`.
- `docker restart` does **not** re-read `EnvironmentFile`/`.env` for the
  bridge; use `compose ... force-recreate` (or recreate the bridge container)
  when the bridge must re-read env. Only recreate the bridge when needed.

## Inspect (safe, read-only)

```
python - <<'PY'
from hermes_mcp_bridge.secret_rotation import inspect_secrets
import json
print(json.dumps(inspect_secrets(), indent=2))
PY
```

`comparable=True` means every present source agrees by digest.

## Plan / apply / verify / rollback

- `plan_rotation(key, new_value=..., force=...)` -> dry plan; aborts if
  active runs or health unknown (fail-closed).
- `apply_rotation(plan)` writes atomically, preserves owner/mode, keeps
  `*.pre-rotation` backups at `0600`. Requires `dry_run=False`.
- `verify_rotation(key)` reports `verified`/`inconclusive` by digest only.
- `rollback_rotation(plan)` restores both sides from `*.pre-rotation`.

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

- [ ] `inspect_secrets()` shows `comparable=True` (or you accept mismatch).
- [ ] No active API runs, or `force=True` authorized.
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

- Wrong source `n8n.env`: effective gateway source empty -> not comparable.
- Empty value hash: empty value yields empty digest, `comparable=False`.
- `docker restart` not re-reading env: runbook mandates force-recreate.
- systemd unit name too long: `_short_unit_name` enforces <=55 chars.
- `set -e` in interactive shell: external script uses `set -euo pipefail`
  only in non-interactive context.
- Gateway in-memory value differs from files: `verify` compares digests and
  reports `inconclusive` on mismatch.
