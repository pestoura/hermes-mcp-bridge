# V2 Phase 2 — out-of-band state-integrity foundation

> **FOUNDATION IMPLEMENTED · NOT WIRED INTO THE CONNECTED GATE · NOT ACCEPTED**
>
> Nothing in this document declares, satisfies or advances a Phase 2 acceptance
> gate. No out-of-band acceptance has been executed. `PHASE2_STATUS` is
> unchanged, V1 and the frozen 27-tool contract are unchanged.

## Problem

The Phase 2 connected gate wants a claim of the form *"the shadow DIRECT run
made zero change to the live Hermes state database"*. That claim cannot be
produced from inside the Hermes control run that is driving the measurement:

* the control run is itself a Hermes session, so it is continuously appending
  to `sessions`, `messages` and `session_model_usage` in its own state DB;
* size and mtime of the database move for reasons unrelated to the shadow run;
* SQLite may checkpoint a WAL at arbitrary moments during a live run.

Therefore **absolute zero delta is only a valid claim out-of-band**: the
before/after measurement must be taken by a process that starts *after* the
Hermes control run has fully ended, and finishes before any new control run
begins. A measurement taken in-band can only ever prove a bounded, explained
delta — never zero.

## Components landed in this branch

| Concern | Location | Default |
| --- | --- | --- |
| read-only measurement | `src/hermes_mcp_bridge/v2/state_integrity.py` | fail-closed |
| transient one-shot planning | `src/hermes_mcp_bridge/v2/out_of_band.py` | dry-run |
| operator CLI (plan/measure) | `scripts/v2_phase2_out_of_band_state_integrity.py` | plan only |
| operator wrapper | `scripts/v2_phase2_out_of_band_state_integrity.sh` | dry-run |
| hermetic tests | `tests/test_v2_phase2_out_of_band_state_integrity.py` | fixtures only |

### Measurement contract

* The database is opened `file:<path>?mode=ro` with `PRAGMA query_only = ON`.
  A test asserts that the measurement leaves the file bytes, size and mtime
  identical and creates no `-wal` / `-shm` / `-journal` sidecar.
* A snapshot records only: `user_version`, SQLite `schema_version`, and per
  tracked table (`sessions`, `messages`, `session_model_usage` **when present**)
  existence, a SHA-256 of the table SQL, `COUNT(*)` and `MAX(rowid)`, plus
  caller-supplied `size_bytes` / `mtime_ns`. Absence of an optional table is
  recorded as `present=false`, never coerced to a zero count.
* No filesystem path, no column value and no row content ever enters a snapshot,
  a comparison, a log line or an exception.
* The snapshot digest is an HMAC keyed by a **per-run in-memory salt**
  (`secrets.token_bytes(32)`). The salt is never returned, written or logged;
  two snapshots from different runs are refused with
  `STATE_SNAPSHOT_SALT_MISMATCH` rather than silently compared.
* The published comparison is booleans and integer deltas only, plus the
  equality result of the salted digest.
* Every failure raises `StateIntegrityError` with a stable code from
  `REASON_CODES`: `STATE_DB_PATH_INVALID`, `STATE_DB_NOT_REGULAR_FILE`,
  `STATE_DB_UNREADABLE`, `STATE_DB_QUERY_FAILED`, `STATE_METADATA_INVALID`,
  `STATE_SALT_INVALID`, `STATE_SNAPSHOT_SCHEMA_MISMATCH`,
  `STATE_SNAPSHOT_SALT_MISMATCH`, `STATE_PATHS_NOT_DISJOINT`.
* `assert_state_paths_disjoint()` fails closed when the shadow database and the
  live database share a file, a sidecar name, a directory, or a parent/child
  directory relationship.

### Orchestration contract

`build_transient_unit_plan()` describes — and never runs — a transient
`systemd-run --user` one-shot:

* `Type=oneshot`, `Restart=no`, `UMask=0077`, bounded `RuntimeMaxSec`,
  `NoNewPrivileges=yes`, `--collect`, delayed start via `--on-active=<N>s`;
* delay and timeout are range-bounded (1–3600s and 1–900s);
* the unit name is regex-validated, the working directory, result path and
  `argv[0]` must be absolute;
* **no `Environment=` and no `--setenv`**, and every argv token is scanned for
  secret-bearing substrings (`token`, `secret`, `password`, `api_key`, `bearer`,
  `authorization`, `private_key`, `pem`, …) — a match is
  `OOB_SECRET_BEARING_ARGUMENT`;
* `schedule_transient_unit()` is dry-run unless both `execute=True` and an
  explicit runner are supplied. The repo test-suite proves the dry-run path
  never executes by passing a runner that raises if invoked.
* The result document is written atomically: temp file in the destination
  directory, `fsync`, `chmod 0600`, `os.replace`. A reader sees the previous
  document or the complete new one; `read_terminal_marker()` returns `None` for
  a partial or foreign-schema document. Only `COMPLETED` and `FAILED` are
  terminal states.
* `cleanup_run_artifacts()` is idempotent and returns basenames only.

The shell wrapper stays dry-run unless `EXECUTE_OUT_OF_BAND=YES` **and**
`--confirm` are both supplied; it accepts paths only and never secret material.

## What this branch deliberately does not do

* It does **not** execute a real out-of-band acceptance run.
* It does **not** register these modules with the V1 server, protocol, tool
  registration path or capability manifest; a test walks `src/` and asserts no
  non-`v2/` module references `state_integrity` or `out_of_band`.
* It does **not** wire the foundation into `scripts/validate_v2_phase2_connected_gate.py`
  or into `v2_phase2_connected_jarvas.sh`. Wiring it before there is runtime
  proof would change Phase 2 semantics on the strength of an unexecuted design.
* It does **not** advance `PHASE2_STATUS`, any ADR status, or the roadmap.

## Required sequence before any acceptance claim

1. End the Hermes control run that would otherwise write to the live state DB.
2. Schedule the transient one-shot with a delay that comfortably exceeds the
   control run's shutdown, using the wrapper with the dual gate.
3. Let the unit take the before/after measurement and write the atomic marker.
4. Read the marker in a *later* control run and evaluate `unchanged`.
5. Only then may an acceptance document quote a zero-delta result, and only for
   the interval covered by the two snapshots.

Any claim of zero delta produced without step 1 is invalid by construction.
