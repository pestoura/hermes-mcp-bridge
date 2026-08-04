# State database backup & restore runbook

Canonical online backup/restore for the Hermes MCP Bridge `state.sqlite3`.
Implementation: `src/hermes_mcp_bridge/state_backup.py`.

## What this guarantees

- Online backup via the SQLite `backup` API (not `cp` over a live WAL file).
- `PRAGMA integrity_check` on source and backup before/after.
- Atomic output: temp file -> `fsync` -> `rename`, mode `0600`.
- Sanitized metadata only: UTC timestamp, schema_migrations count/version,
  sizes, SHA-256 **prefix** (<=16 chars), owner/mode, bridge version.
  No row data, no secret material.
- Nested output paths are created automatically.
- Retention and dry-run supported.

## Backup

```
python - <<'PY'
from hermes_mcp_bridge.state_backup import backup_state_db
r = backup_state_db(retention_count=7, dry_run=False)
print(r["status"], r["backup"], r["metadata"]["source_sha256_prefix"])
PY
```

- `dry_run=True` returns metadata preview without writing the backup file.
- `retention_count=N` keeps the N newest `*.backup.*` files in the target dir.

## Restore (fail-closed)

```
python - <<'PY'
from hermes_mcp_bridge.state_backup import restore_state_db
r = restore_state_db("/path/to/state.sqlite3.backup", force=True)
print(r["status"], r["previous_target_backup"])
PY
```

Rules:

- Refuses if an active writer is detected, unless `force=True`.
- If `require_bridge_stopped=True`, also refuses while the bridge is active.
- Validates backup `integrity_check` before replacing the target.
- Preserves the current target as `*.pre-restore-<timestamp>` (mode `0600`).
- `fsync` on file and directory after replace.

## Pre-checklist

- [ ] Confirm the bridge is stopped OR you will pass `force=True` deliberately.
- [ ] Confirm the backup path exists and `integrity_check` passes
      (`verify_backup`).
- [ ] Confirm schema version compatibility (`backup_schema_version`
      must not exceed `source_schema_version`).
- [ ] Confirm free disk space for temp file + target + pre-restore copy.

## Post-checklist

- [ ] `verify_backup(backup, source)` reports `backup_integrity=True`.
- [ ] Bridge starts and `health` returns 200.
- [ ] Old `*.pre-restore-*` copy is retained until verified good.

## Decision

- `PASS`: backup + restore verified, bridge healthy, pre-restore retained.
- `PARTIAL`: restore applied but health/verify inconclusive; keep bridge stopped.
- `FAIL_ROLLED_BACK`: restore failed; original target restored from
  `*.pre-restore-*` automatically.

## Incident notes

- Do **not** `cp` a live `.sqlite3-wal`/`-shm` pair; use the backup API.
- Never restore over a running writer without `force=True`; prefer stopping
  the bridge first.
