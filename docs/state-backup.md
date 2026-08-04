# State database backup & restore runbook

Canonical online backup/restore for the Hermes MCP Bridge `state.sqlite3`.
Implementation: `src/hermes_mcp_bridge/state_backup.py`.

## What this guarantees

- Online backup via the SQLite `backup` API (not `cp` over a live WAL file).
- `PRAGMA integrity_check` on source and backup before/after.
- Atomic output: temp file -> `fsync` -> `rename`, mode `0600`.
- Sanitized metadata only: UTC timestamp, schema_migrations count/version,
  sizes, SHA-256 **prefix** (<=16 chars), owner/mode, bridge version, and an
  `online_backup` flag. No row data, no secret material.
- Nested output paths are created automatically.
- Default backups are **unique** (`YYYYmmddThhmmssZ-<nonce>`); an explicit
  `backup_path` that already exists fails closed unless `overwrite=True`.
- Retention and dry-run supported.

## Backup

```
python - <<'PY'
from hermes_mcp_bridge.state_backup import backup_state_db
r = backup_state_db(retention_count=7)
print(r["status"], r["backup"], r["metadata"]["source_sha256_prefix"])
PY
```

- `dry_run=True` returns metadata preview without writing any file or temp.
- `retention_count=N` keeps the N newest `*.backup-*` files in the target dir.
- The backup is taken while writers may be active; the backup API produces a
  consistent snapshot and `online_backup=True` is recorded in metadata.

## Restore (fail-closed)

```
python - <<'PY'
from hermes_mcp_bridge.state_backup import restore_state_db
r = restore_state_db("/path/to/state.sqlite3.backup", force=True)
print(r["status"], r["previous_target_backup"])
PY
```

Rules:

- Writer state is classified deterministically via SQLite locking:
  `clear`, `active`, or `unknown`. Any state other than `clear` blocks the
  restore unless `force=True`.
- If `require_bridge_stopped=True`, also refuses while the target is `active`.
- The current target and its `-wal`/`-shm` sidecars are moved into a single
  rollback bundle `*.pre-restore-<ts>-<nonce>` (mode `0600`). Stale sidecars
  without a db are removed so they cannot attach to the restored db.
- Validates backup `integrity_check`, copies via backup API, then `fsync`
  file + directory after replace.
- On any failure, the previous target bundle is restored automatically
  (internal rollback).

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
- [ ] Old `*.pre-restore-*` bundle is retained until verified good.

## Decision

- `PASS`: backup + restore verified, bridge healthy, pre-restore retained.
- `PARTIAL`: restore applied but health/verify inconclusive; keep bridge stopped.
- `FAIL_ROLLED_BACK`: restore failed; original target restored from the
  `*.pre-restore-*` bundle automatically.

## Incident notes

- Do **not** `cp` a live `.sqlite3-wal`/`-shm` pair; use the backup API.
- Never restore over a running writer without `force=True`; prefer stopping
  the bridge first.
- Sidecar files (`-wal`/`-shm`) are always handled together with the db to
  avoid attaching stale WAL logs to a restored database.
