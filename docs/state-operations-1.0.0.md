# Hermes MCP Bridge 1.0.0 — state operations

## Scope

This runbook covers the bridge-owned SQLite database, normally mounted at:

```text
/var/lib/hermes-mcp-bridge/state.sqlite3
```

It does **not** inspect or mutate the RITMO database. RITMO run leases and task
lifecycle remain owned by the RITMO connector and its APIs.

The bridge database contains operational mappings and coordination state such
as run mappings, approvals, checkpoints, sagas and resource locks. Diagnostic
outputs are aggregate and never expose IDs, fingerprints, prompts, outputs,
resources, owners or secret material.

## Read-only diagnostic

`diagnose_state_db()` opens SQLite using `mode=ro` and `query_only=ON` and
returns:

- `PRAGMA quick_check` status;
- migration version and count;
- journal mode, page size/count, freelist count and WAL autocheckpoint setting;
- database, WAL and SHM sizes;
- host free bytes, owner UID and file mode;
- row counts for a fixed allow-list of known tables;
- run counts by status and aggregate stale non-terminal counts;
- aggregate active/expired resource-lock counts;
- aggregate pending/expired approval counts;
- aggregate running/stale saga counts;
- a checkpoint recommendation based only on WAL size;
- a fixed warning vocabulary.

No SQL mutation, checkpoint or cleanup is performed by the diagnostic path.

### Default warning thresholds

```text
stale non-terminal state: 3600 seconds
WAL size:                 64 MiB
database size:             2 GiB
free disk:                 5 GiB
```

Threshold overrides are inputs to the diagnostic call, not environment-backed
security bypasses. Negative values and a non-positive stale window are rejected.

## Interpreting warnings

| Warning | Meaning | Initial action |
| --- | --- | --- |
| `quick_check` | SQLite quick check failed | Stop rollout and create evidence; do not checkpoint blindly |
| `database_size` | Main DB reached threshold | Review retention and table growth |
| `wal_size` | WAL reached threshold | Plan a controlled checkpoint |
| `disk_free` | Host free space reached threshold | Free or extend storage before maintenance |
| `stale_runs` | Non-terminal mappings exceeded age threshold | Compare with Hermes upstream state; do not delete automatically |
| `stale_locks` | Active locks are expired or malformed | Review lock lifecycle through bridge APIs |
| `expired_approvals` | Pending approvals passed expiry | Review approval lifecycle; no direct SQL cleanup |
| `stale_sagas` | Running/compensating sagas exceeded age threshold | Reconcile through saga APIs |

Diagnostics deliberately do not return the affected identifiers. Investigation
requiring identifiers must use the relevant authenticated bridge tool or an
explicitly approved offline procedure.

## WAL checkpoint

`checkpoint_state_db()` is separated from diagnostics and defaults to:

```text
execute=false
mode=TRUNCATE
```

The dry plan reports only mode, writer state and WAL bytes. Execution requires:

1. explicit `execute=true`;
2. a supported mode: `PASSIVE`, `FULL`, `RESTART` or `TRUNCATE`;
3. clear writer state by default;
4. acquisition of the state-maintenance file lock;
5. SQLite's own checkpoint result with `busy=0` for success.

The function never edits tables. It does not automatically retry, stop the
bridge or bypass an active/unknown writer. A caller can disable the writer-clear
precondition only through an explicit API argument; production runbooks must not
do so.

For production `TRUNCATE`, stop or quiesce the bridge through the controlled
deployment procedure first. A checkpoint result of `busy` is not success.

## Backup

Use `backup_state_db()` from `state_backup.py`:

- SQLite online backup API;
- atomic temporary output and `os.replace`;
- `0600` result;
- fsync of file and directory;
- integrity verification;
- sanitized metadata;
- optional retention that removes only module-generated backup names.

An explicit existing target fails closed unless overwrite was explicitly
requested. Backups are not a substitute for a restore proof.

## Isolated restore proof

`verify_restore_in_isolation()` exercises the real `restore_state_db()` path in
a temporary target under the backup parent directory. It verifies:

- backup path safety;
- actual SQLite backup-to-target restore;
- full `PRAGMA integrity_check` on the restored target;
- migration version/count;
- presence of schema tables;
- non-empty restored database;
- cleanup of the temporary target and sidecars.

The proof does not replace the production database and does not require the
production bridge to stop because the target is isolated.

Required decision marker:

```text
HERMES_BRIDGE_1_0_0_ISOLATED_RESTORE_PASS
```

## Production restore

A production restore remains a separate emergency operation:

1. identify the exact trusted backup and evidence;
2. stop the bridge through the controlled deployment path;
3. confirm writer state is clear;
4. retain the current DB/WAL/SHM rollback bundle;
5. call `restore_state_db(..., require_bridge_stopped=true)`;
6. run integrity and contract validation;
7. start the bridge and verify readiness;
8. retain sanitized evidence.

`force=true` never bypasses path safety. It must not be used to mask an unknown
writer state during normal recovery.

## Retention

Retention applies only to names generated by the backup module. External files,
manually named evidence and unrelated SQLite files are never deleted.

Before enabling retention in production:

- define the minimum number of known-good backups;
- account for available disk and backup growth;
- preserve release/SBOM evidence separately;
- prove the oldest removable candidate in dry-run or an isolated directory;
- record deletion counts, not sensitive paths, in central telemetry.

## Acceptance

The state operations block is accepted only when CI proves:

- read-only diagnostics do not change the main DB;
- output contains no seeded identifiers or paths;
- stale runs, locks, approvals and sagas are counted correctly;
- checkpoint defaults to dry-run;
- active writers block checkpoint execution;
- a clear WAL can be checkpointed and truncated;
- an online backup restores through the real isolated path;
- corrupted backups fail closed;
- temporary restore targets are removed;
- Python 3.11/3.12, Ruff, ShellCheck, runtime build, Trivy and SBOM gates pass.

Development decision marker:

```text
HERMES_BRIDGE_1_0_0_STATE_OPERATIONS_PASS
```

This marker does not authorize production deployment and does not replace the
Hermes/RITMO single-slot acceptance.
