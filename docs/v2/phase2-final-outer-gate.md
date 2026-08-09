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
