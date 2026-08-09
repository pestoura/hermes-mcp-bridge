# V2 Phase 2 — OUTER final gate (integrity + provenance)

> **IMPLEMENTED · REQUIRED FOR FORMAL ACCEPTANCE · STATUS STILL NOT ACCEPTED**
>
> This branch adds the final assurance layer. It does **not** declare a Phase 2
> acceptance. No real out-of-band Jarvas run has been executed, so
> `overall_status` remains **NOT ACCEPTED** until one passes. V1 and the frozen
> 27-tool contract are unchanged.

## Two gates, not one

The existing connected launcher/gate is preserved unchanged as the **INNER**
gate. It stays the semantic/economics gate and stays independently runnable and
debuggable, so current evidence keeps its meaning.

```text
INNER  (unchanged)                    OUTER (new, stricter)
scripts/v2_phase2_connected_jarvas.sh scripts/v2_phase2_final_out_of_band_acceptance.py
  → 15 samples, DIRECT vs V1 shadow     → guard: no control activity
  → normalized semantic digests         → PRE real-state snapshot (read-only)
  → real token accounting               → invoke the INNER launcher unchanged
  → DIRECT_READ_ACCEPTED                → POST real-state snapshot (read-only)
                                        → absolute zero delta or block
                                        ↓
                            scripts/build_v2_phase2_final_evidence.py
                            scripts/validate_v2_phase2_final_acceptance.py
                                        ↓
                                  overall_status = ACCEPTED
```

The OUTER gate is **REQUIRED** for a formal `ACCEPTED`. An inner
`DIRECT_READ_ACCEPTED` alone is no longer sufficient.

## A. Internal-tool provenance

Module: `src/hermes_mcp_bridge/v2/tool_provenance.py`.

For each *already accepted* connected sample, and scoped strictly to that
sample's shadow session id, the collector recovers from the **disposable shadow**
state database exactly one authorized GitHub MCP tool call plus its matching
result, and proves the internal result normalizes to the same digest as DIRECT.

It fails closed with a stable code on:

| Condition | Code |
| --- | --- |
| no authorized call | `PROVENANCE_NO_AUTHORIZED_TOOL_CALL` |
| more than one authorized call | `PROVENANCE_MULTIPLE_AUTHORIZED_TOOL_CALLS` |
| any unauthorized tool name | `PROVENANCE_UNAUTHORIZED_TOOL_CALL` |
| wrong tool for the sample | `PROVENANCE_TOOL_MISMATCH` |
| argument shape differs | `PROVENANCE_ARG_SHAPE_MISMATCH` |
| target/arguments differ | `PROVENANCE_TARGET_MISMATCH` |
| result missing / unparseable | `PROVENANCE_RESULT_MISSING`, `PROVENANCE_RESULT_UNPARSEABLE` |
| internal digest ≠ DIRECT | `PROVENANCE_DIGEST_MISMATCH` |

**Persisted:** booleans, canonical tool id, normalized digests, the
normalization profile id, a coarse result size bucket and stable blocker codes.

**Never persisted:** `tool_call_id`, raw arguments or values, raw result,
session id, message rows, any filesystem path. A privacy test asserts none of
those strings can appear in the emitted record.

**Ordering rule:** the final LLM semantic digest/match is evaluated *first* and
remains the hard gate. Provenance runs only on a sample that already matched, so
a provenance PASS can never turn a semantic FAIL into a PASS. A test asserts
this.

Aggregate requirement: `provenance_pass = 15`, `provenance_fail = 0`.

## B. State integrity, out-of-band

Runner: `scripts/v2_phase2_final_out_of_band_acceptance.py`, built on the
already merged `state_integrity.py` / `out_of_band.py` foundation.

The runner is designed for **transient user-systemd** execution *after* the
scheduling Hermes control run has ended:

1. **Guard.** Before PRE it inspects the real state DB for active API
   runs/delegations. Any activity → `FINAL_CONTROL_ACTIVITY_DETECTED`. A guard
   that cannot be evaluated → `FINAL_CONTROL_GUARD_UNAVAILABLE`; it is never
   treated as "no activity".
2. **PRE.** Read-only snapshot of the **real** Hermes state database.
3. **Inner.** Invokes the existing inner connected launcher, unchanged.
4. **POST.** Read-only snapshot again, same per-run salt.
5. **Verdict.** Requires, with **no exclusions or allowlists of any control
   session**: row deltas of `sessions`, `messages` and `session_model_usage` all
   exactly `0`; stable `user_version` and SQLite `schema_version`; stable file
   size and mtime; and equal, non-empty per-run salted fingerprints.

The **shadow** database is allowed and expected to change. Positive shadow
activity is proven as a boolean plus a row-count delta captured **before** the
inner launcher's cleanup, alongside the existing empirical token rows. Row
contents are never read into evidence.

No path, row content, salt or session id appears in the emitted document.

## C. Final evidence, validator and manifest

New schemas, no reuse of old fields:

| Artifact | Schema |
| --- | --- |
| final evidence | `hermes-v2-phase2-final-acceptance/1` |
| state-integrity section | `hermes-v2-phase2-final-state-integrity/1` |
| manifest | `hermes-v2-phase2-final-manifest/1` |
| provenance record | `hermes-v2-phase2-tool-provenance/1` |

`overall_status = ACCEPTED` requires **all** of:

* inner `direct_read_status = DIRECT_READ_ACCEPTED` with no inner failures;
* `source_commit` exact and identical across every document;
* `sample_count = 15`, `successful_samples = 15`, `semantic_matches = 15`;
* `provenance_pass = 15`, `provenance_fail = 0`, and 15 passing records;
* `token_measurement_mode = empirical`;
* `direct_total_tokens = 0`, `agentic_total_tokens > 0`,
  `token_reduction_percent >= 80`;
* `direct_provider_api_calls = 15`, `mutations_observed = 0`;
* live real-state fingerprints before/after non-empty and equal;
* real `sessions` / `messages` / `session_model_usage` row deltas all `0`;
* live schema/user versions and file size/mtime stable;
* `shadow_state_activity_observed = true` with a positive row-count delta;
* the state measurement window encloses the inner samples;
* `paths_stored = false`, `row_contents_stored = false`.

A **missing or unmeasurable state-integrity document hard-blocks** final
acceptance (`state_integrity_document_missing`). Only stable sanitized reasons
are emitted; the validator never reads or promotes the probe's raw
stdout/stderr.

## D. Out-of-band orchestration

`plan` mode prints the sanitized `systemd-run --user --on-active=<delay>s`
one-shot. Enforced properties: `Type=oneshot`, `Restart=no`, `UMask=0077`,
bounded `RuntimeMaxSec`, `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome=read-only`.

* **No credentials or secrets** in argv, in `Environment=`, in `--setenv` or in
  the unit name — asserted by tests. Canonical file-backed credentials continue
  to be read by the inner launcher under the operator's own identity.
* Result/status files are written atomically at mode `0600`; cleanup is
  idempotent.
* Any retained raw stdout/stderr must be `0600` and is **never** read or
  promoted by the final validator.
* `plan` is the default. Real execution requires **both** the internal
  `--i-understand-this-runs-a-real-acceptance` flag and
  `HERMES_V2_FINAL_EXECUTE=YES`. **Nothing was scheduled in this code run.**

## Operator sequence (not executed here)

```bash
# 1. print the plan (safe, runs nothing)
python3 scripts/v2_phase2_final_out_of_band_acceptance.py plan \
  --state-db "$HOME/.hermes/state.db" \
  --shadow-state-db "$HOME/.hermes-v2-acceptance/shadow-hermes-runtime/state.db" \
  --inner-launcher "$PWD/scripts/v2_phase2_connected_jarvas.sh" \
  --result "$HOME/.hermes-v2-acceptance/final-oob-result.json" \
  --working-directory "$PWD" \
  --source-commit "$(git rev-parse HEAD)"

# 2. schedule it with systemd-run, then END the Hermes control run
# 3. after it completes, assemble and validate
python3 scripts/build_v2_phase2_final_evidence.py \
  --evidence  ".../phase2-connected-evidence.json" \
  --inner-gate ".../phase2-connected-gate.json" \
  --state-marker ".../final-oob-result.json" \
  --json-out ".../phase2-final-evidence.json"

python3 scripts/validate_v2_phase2_final_acceptance.py \
  ".../phase2-final-evidence.json" --json-out ".../phase2-final-manifest.json"
```

## Status

**NOT ACCEPTED.** The code path is implemented and hermetically tested. Formal
`ACCEPTED` requires a real out-of-band Jarvas run producing a passing manifest,
which has not been performed.

## Repaired blockers (final OOB acceptance, `fix/v2-phase2-oob-final-runner-blockers`)

Three confirmed blockers found in the final out-of-band acceptance dry review,
plus one background-state validity finding.

### A) shadow positive control survives inner cleanup

The inner launcher deletes `SHADOW_HOME` in its `EXIT` trap, so the outer
runner could never observe post-run shadow activity. Cleanup is **not**
disabled. Instead an acceptance-only private handoff was added:

* the outer runner exports `HERMES_V2_FINAL_SHADOW_WITNESS_FILE=<abs path>`
  when it invokes the inner launcher — nobody else sets it, and with the
  variable unset the mechanism is completely inert;
* the launcher captures a bounded `COUNT(*)` baseline before the shadow
  runtime does any work, and, in `cleanup()` **before** `rm -rf "$SHADOW_HOME"`,
  writes a sanitized `0600` witness to that path (rejected at startup if the
  path is relative or lives inside `SHADOW_HOME`);
* the witness contains only schema/version, the pinned `source_commit`, the
  `sessions` / `session_model_usage` row-count deltas, and booleans proving
  positive growth, that the shadow DB was a *different* file from the real
  state DB (`st_dev`/`st_ino`), and that it lived inside the disposable home.
  No path, row content, session id, token or prompt is ever serialized;
* the outer runner validates schema, version and exact commit, **consumes the
  file once** (it is unlinked whether or not it validated, so it can never be
  replayed) and blocks with `FINAL_SHADOW_WITNESS_INVALID` otherwise.

The `--shadow-row-count-after` CLI override was **removed**: no caller-supplied
positive-control value can be fabricated any more.

### B) minimal, explicit systemd write access

`ProtectHome=read-only` blocked the canonical acceptance workspace.
`ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges=yes`,
`PrivateTmp=yes` and `UMask=0077` all stay on. Write access is now granted
explicitly and minimally through `ReadWritePaths`, for exactly two canonical,
non-secret directories: the acceptance working directory and the directory that
receives the result marker. The credential source (the real Hermes home
containing `state.db`) is mounted through `ReadOnlyPaths` and can never appear
as a writable grant. `$HOME`, `/`, `/home`, `/etc`, `/var`, `/root` and any
relative or secret-bearing path are rejected at plan-build time
(`OOB_SANDBOX_PATH_TOO_BROAD`, `OOB_PATH_NOT_ABSOLUTE`,
`OOB_SANDBOX_PATH_CONFLICT`, `OOB_SECRET_BEARING_ARGUMENT`). Tests assert no
write access to arbitrary home and no secret in argv, environment or unit
properties.

### C) control-activity guard against the real Hermes 0.20 schema

The old guard probed `api_runs` and `delegations`. Neither exists in Hermes
0.20 (`schema_version = 25`): every probe was skipped and the guard silently
answered "quiet" — the exact failure a guard must not have.
`hermes_mcp_bridge.v2.control_activity` now introspects the real tables
fail-closed (`sessions`, `async_delegations`, `delivery_obligations`,
`compression_locks`) with the columns it depends on, and reports
`QUIET` / `ACTIVE` / `UNMEASURABLE`:

* `ACTIVE` — an in-flight `async_delegations.state`/`delivery_state`, a pending
  `delivery_obligations.state`, a held `compression_locks` row, or a session
  whose `last_activity_at` heartbeat is inside the recency window (the 0.20
  replacement for the non-existent `api_runs` table);
* `UNMEASURABLE` — missing table, missing column, unreadable DB or failed
  query. It is **never** downgraded to quiet; the runner aborts with
  `FINAL_CONTROL_GUARD_UNAVAILABLE`.

Only bounded aggregates are read (`COUNT(*)` over closed vocabularies, one
timestamp comparison). No row content or identifier is read or reported.

### D) background-state validity — `FINAL_BACKGROUND_WRITER_UNCONTROLLED`

Audited **read-only against the installed Hermes 0.20 source**; no gateway was
stopped, restarted, or measured live, and no acceptance was executed.

Finding: an idle real `hermes-gateway.service` is **not** provably write-free
against the tracked tables during a short no-op window. Two periodic,
non-request-driven writers touch `sessions`:

* `gateway/run.py` housekeeping tick → `SessionDB.maybe_auto_archive()` →
  `archive_stale_sessions()` → `set_session_archived()` flips
  `sessions.archived` and writes `state_meta['last_auto_archive']`;
* `gateway/run.py::_session_expiry_watcher` (300 s cadence) →
  `set_expiry_finalized()` writes `sessions.expiry_finalized`.

Both are timers, so an idle gateway can write inside the measurement window.
The auto-archive path is config-gated (`sessions.auto_archive`, currently unset
on this host) but that is a mutable runtime setting, not a code guarantee, and
the expiry watcher is not gated at all. Code evidence therefore **cannot**
prove an idle gateway is write-free.

Consequently the runner adds an explicit precondition rather than proceeding
silently: `execute` aborts with `FINAL_BACKGROUND_WRITER_UNCONTROLLED` unless
the operator passes `--background-writer-controlled`. **Zero absolute delta is
not relaxed in any way.**

What must be controlled before scheduling the real acceptance:

1. the real `hermes-gateway.service` must be stopped (or otherwise proven not
   to hold the tracked DB) for the entire PRE→inner→POST window; and
2. no Hermes cron job, kanban dispatch, delivery retry or CLI session may run
   in that window; and
3. that control must be established **out of band**, from outside any Hermes
   control run — which is exactly why this repository never schedules it.
