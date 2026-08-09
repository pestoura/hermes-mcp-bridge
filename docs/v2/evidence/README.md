# V2 Acceptance Evidence Index

> **Phase 0:** `BASELINE_ACCEPTED` · **Phase 1:** `REGISTRY_ACCEPTED` ·
> **Phase 2:** `DIRECT_READ_ACCEPTED` (inner) + outer `ACCEPTED` ·
> **Phase 3:** `DIRECT_MUTATION_ACCEPTED` · **Phase 4:** `BATCH_ACCEPTED` ·
> **Phase 5:** `DAG_ACCEPTED` · **V1 semantics preserved**

This directory indexes retained, sanitized evidence for the gated V2 evolution.
Acceptance is evidence-driven: code existence alone does not promote a phase.
No evidence retained here contains prompt text, output text, raw credentials,
secret values or authentication/environment paths.

## Phase 0 — connected baseline

### Runtime under test

| Item | Value |
| --- | --- |
| Bridge version | `1.0.0` |
| Schema version | `0.6.1` |
| Upstream Hermes status | `ok` |
| Base commit (`main`) | `f0b7e72f6bdf42e82712f3d2e8182ff937ae9509` |
| Collection window | 2026-08-08T04:07:42Z → 2026-08-08T04:09:38Z |

### Evidence files and digests

| File | SHA-256 |
| --- | --- |
| `phase0-connected-baseline-20260808.json` | `ea6cc080891d133fa835a4e852f69dd124feaeccc38befe24293395020667559` |
| `phase0-connected-baseline-gate-20260808.json` | `8db24e8436fcac4b2a9eae2f41ad5b8e9c5194a0f174a3b0f3f67e612e0f9997` |

`phase0-scenarios.example.json` remains a non-evidence scenario template.

### Result summary

Three required categories, three repetitions each: 9/9 successful, 0 failures,
0 contaminated metric windows and `bridge_execution_terminal_total` delta
exactly `1` in every repetition.

| Category | Scenario ID | Runs | Successes | Tokens (total) |
| --- | --- | --- | --- | --- |
| `read` | `github_read_repo_metadata` | 3 | 3 | 181,063 |
| `mutation` | `local_disposable_roundtrip` | 3 | 3 | 246,111 |
| `agentic` | `bounded_execution_mode_reasoning` | 3 | 3 | 91,874 |
| **Total** | — | **9** | **9** | **519,048** |

Token counts are real accounting from the Hermes execution result path, not
character-based estimates. Mutation cleanup residual count was `0`.

### Traceability

```text
requirements
  -> scripts/v2_phase0_benchmark.py
  -> phase0-connected-baseline-20260808.json
  -> scripts/validate_v2_phase0_evidence.py
  -> phase0-connected-baseline-gate-20260808.json
  -> BASELINE_ACCEPTED
```

## Phase 1 — canonical Tool Registry

### Accepted implementation

| Item | Value |
| --- | --- |
| Source commit accepted | `4bc999084b88cc5ef5346f21c9f2e09717c63568` |
| GitHub Actions run | `31254605844` (`CI` #189) |
| Workflow conclusion | `success` |
| Gate | `REGISTRY_ACCEPTED` |
| Gate failures | `[]` |
| V1 contract | 27 tools, unchanged and unwired from V2 |

The source commit above is the integrated `main` commit that contains the Phase
1 core plus the acceptance collector/validator. Its CI passed Python 3.11/3.12,
full tests, Phase 1 evidence generation/validation/retention, image provenance,
isolated Docker acceptance, Trivy and CycloneDX/SBOM retention.

### Durable evidence

Phase 1 evidence is retained as a **blocking draft GitHub Release**, not an
ephemeral workflow artifact. The release is targeted to the exact accepted
commit and the CI verifies that both required assets were uploaded before the
workflow may continue.

| Item | Value |
| --- | --- |
| Release ID | `367184431` |
| Release tag | `phase1-registry-evidence-4bc999084b88cc5ef5346f21c9f2e09717c63568` |
| `target_commitish` | `4bc999084b88cc5ef5346f21c9f2e09717c63568` |
| Draft | `true` |
| `phase1-registry-acceptance.json` | 5323 bytes · SHA-256 `ab66fc6dd872d2f184dafea6566dfcba178d7328f87eda9f9e319da8f030c20a` |
| `phase1-registry-gate.json` | 115 bytes · SHA-256 `4acaa6699b5374176f8b63b5be40d05b0fabbcfbea8624c5a3beb8f48ab78d1a` |

`phase1-registry-acceptance-release-20260808.json` is the repository retention
manifest for this release and its digests. It deliberately references the
original release assets rather than duplicating them from logs.

The 115-byte gate asset is the deterministic JSON result for:

```json
{
  "failures": [],
  "gate": "REGISTRY_ACCEPTED",
  "source_commit": "4bc999084b88cc5ef5346f21c9f2e09717c63568"
}
```

Its canonical file bytes recompute to the SHA-256 recorded above.

### Acceptance scope

The fail-closed Phase 1 gate proves the requirements mapped to
`REGISTRY_ACCEPTED` in `docs/v2/requirements/traceability-matrix.md`, including:

- canonical typed registry/schema invariants;
- deterministic, versioned `capability_snapshot_hash` and material-change
  detection;
- exact capability readiness semantics;
- fail-closed policy decisions and stable reason codes;
- destructive/T4 default deny before permissive policy;
- authorized-only deterministic projection with a strict output allow-list;
- credential capability/status-only contract without credential material;
- rejection of materialized sensitive schema values and wildcard policy rules;
- exclusion of free-text editorial metadata from canonical snapshots/projection;
- V1 isolation and the unchanged 27-tool contract.

The gate does **not** claim implementation of the explicitly deferred items:
registry persistence/signing, a real credential backend, principal/tenant
authorization, dynamic discovery/projection or the later durable policy engine.

### Traceability

```text
Phase 1 requirements
  -> tests/test_v2_phase1_registry_core.py
  -> scripts/v2_phase1_registry_acceptance.py
  -> scripts/validate_v2_phase1_registry_evidence.py
  -> draft release phase1-registry-evidence-<accepted-main-sha>
  -> phase1-registry-acceptance-release-20260808.json
  -> REGISTRY_ACCEPTED
```

## Phase 2 — GitHub DIRECT read-only (connected + out-of-band)

> **Status: ACCEPTED.** Inner gate `DIRECT_READ_ACCEPTED` with `failures=[]`;
> outer manifest `overall_status=ACCEPTED` with `reasons=[]`.

Two independent gates were required and both passed; their conditions are
authoritative in the runbooks and are not restated here:

- inner semantic/economics gate: [`../phase2-connected-acceptance.md`](../phase2-connected-acceptance.md);
- outer integrity/provenance gate: [`../phase2-final-outer-gate.md`](../phase2-final-outer-gate.md).

### Accepted implementation

| Item | Value |
| --- | --- |
| Accepted source commit | `818c56a467ed00b1412a219c78e3c68007848df3` |
| GitHub Actions run for that commit | `31299055311` (`CI`), conclusion `success` |
| Bridge version / schema version | `1.0.0` / `0.6.1` |
| V1 contract during the window | 27 tools, unchanged |
| Credential provider type | `github_app`, least-privilege, `broad_pat=false` |
| Inner gate | `DIRECT_READ_ACCEPTED`, `failures=[]` |
| Outer manifest | `overall_status=ACCEPTED`, `reasons=[]` |

### Retained evidence files and digests

| File | SHA-256 |
| --- | --- |
| `phase2-final-evidence-20260809.json` | `2918de02df386f2d1182422ee62596ffb6f207b40939566d164552eb31d63c9d` |
| `phase2-final-manifest-20260809.json` | `ca104f340a841145f5f03d5b51cc341ef2c21deb03c5a359f14499eae19327fb` |

The final evidence envelope embeds the sanitized inner-gate summary, the
aggregate economics, the 15 per-sample provenance records and the out-of-band
state-integrity section, so it is the single retained document for this phase.

### Measured aggregates

| Field | Recorded value |
| --- | --- |
| `sample_count` / `successful_samples` / `semantic_matches` | 15 / 15 / 15 |
| `provenance_pass` / `provenance_fail` | 15 / 0 |
| `token_measurement_mode` | `empirical` |
| `direct_total_tokens` | 0 |
| `agentic_total_tokens` | 13784 |
| `token_reduction_percent` | 100.0 |
| `direct_provider_api_calls` | 15 |
| `mutations_observed` | 0 |
| Upstream Hermes LLM calls on the DIRECT path | 0 |
| Contaminated metric windows | 0 |

### Out-of-band real-state integrity

| Field | Recorded value |
| --- | --- |
| `measured_out_of_band` / `read_only` | `true` / `true` |
| `fingerprint_before` | `c166f913eb3123ab030c0d7c2211fde2824b7ba8e84e5e96c88d893db1cc5af5` |
| `fingerprint_after` | `c166f913eb3123ab030c0d7c2211fde2824b7ba8e84e5e96c88d893db1cc5af5` |
| Row deltas `sessions` / `messages` / `session_model_usage` | 0 / 0 / 0 |
| `size_changed` / `mtime_changed` / `user_version_changed` | `false` / `false` / `false` |
| `shadow_state_activity_observed` / `shadow_row_count_delta` | `true` / 30 |
| `shadow_db_distinct_from_source` / `shadow_db_disposable` | `true` / `true` |
| `measurement_self_write_observed` | `false` |
| Writers restored after the window | `true` |

The positive shadow witness with a strictly zero real-state delta is what
distinguishes a genuine isolated run from an unobserved one.

### Retention rules for this section

1. Values are written here only after the corresponding validator ran against
   the retained document for the exact commit under test.
2. Evidence produced for an earlier commit is never carried forward; a new run
   produces new files and new digests.
3. No value may be transcribed from CI mocks, fixtures, examples or a
   validator's own defaults.
4. `DIRECT_READ_ACCEPTED` and the outer `ACCEPTED` status are declared by the
   validators, never by editing this document.
5. Privacy controls are identical to the earlier phases: no prompt text, output
   text, credential values, session ids, tool call ids, row contents or
   filesystem paths.

### Traceability

```text
Phase 2 requirements (docs/v2/requirements/traceability-matrix.md)
  -> tests/test_v2_phase2_*.py
  -> scripts/v2_phase2_connected_jarvas.sh                 [INNER, RUN]
  -> scripts/validate_v2_phase2_connected_gate.py          [DIRECT_READ_ACCEPTED]
  -> scripts/v2_phase2_final_out_of_band_acceptance.py     [OUTER, RUN]
  -> scripts/build_v2_phase2_final_evidence.py             [RUN]
  -> scripts/validate_v2_phase2_final_acceptance.py        [ACCEPTED]
  -> phase2-final-evidence-20260809.json                   [RETAINED]
  -> phase2-final-manifest-20260809.json                   [RETAINED]
  -> DIRECT_READ_ACCEPTED                                  [DECLARED]
  -> outer overall_status                                  [ACCEPTED]
```

## Phase 3 — GitHub mutations (`DIRECT_MUTATION_ACCEPTED`)

| Item | Value |
| --- | --- |
| Accepted source commit | `8fc8363a3eb31db99c18afb39fcd78bde011e2b6` |
| Gate | `DIRECT_MUTATION_ACCEPTED`, `failures=[]` |
| Evidence | `phase3-direct-mutation-acceptance.json` |
| Runner and criteria mapping | [`../phase3/promotion.md`](../phase3/promotion.md) |

The evidence document binds each Phase 3 module by SHA-256 against the tree at
the accepted commit, so a later edit to a bound module invalidates the record
rather than silently inheriting the acceptance.

## Phase 4 — BATCH engine (`BATCH_ACCEPTED`)

| Item | Value |
| --- | --- |
| Accepted source commit | `0f1814793169d8641e8a4223779a9c5d31a3d2ca` |
| Gate | `BATCH_ACCEPTED`, `failures=[]` |
| Live `max_observed_inflight` | `2` (real parallel execution, not a claim) |
| Bound modules | `v2/batch_contract.py`, `v2/batch_scheduler.py` |
| Evidence | `phase4-batch-acceptance.json` |
| Runner and semantics | [`../phase4/promotion.md`](../phase4/promotion.md) |

The runtime ships behind `BATCH_FEATURE_ENABLED = False` and is not wired to
MCP. The V1 contract was unchanged during the window: `1.0.0` / `0.6.1` / 27
tools.

### Traceability

```text
Phase 4 requirements
  -> tests/test_v2_phase4_batch_scheduler.py             [S-01..S-27, RUN]
  -> scripts/validate_v2_phase4_batch_gate.py            [INNER + OUTER]
  -> phase4-batch-acceptance.json                        [RETAINED]
  -> BATCH_ACCEPTED                                      [DECLARED]
```

## Phase 5 — DAG engine (`DAG_ACCEPTED`)

| Item | Value |
| --- | --- |
| Gate | `DAG_ACCEPTED`, `failures=[]` |
| Bound modules | `v2/dag_contract.py`, `v2/dag_transform.py`, `v2/dag_digest.py`, `v2/dag_validation.py`, `v2/dag_store.py`, `v2/dag_engine.py`, `v2/dag_loader.py` |
| Acceptance suite | `tests/test_v2_phase5_dag_acceptance.py` (A5-01..A5-22, executed for real) |
| Evidence | `phase5-dag-acceptance.json` |
| Runner and criteria mapping | [`../phase5/acceptance-criteria.md`](../phase5/acceptance-criteria.md) |

The runtime ships behind `DAG_FEATURE_ENABLED = False` and is **not** wired to
MCP. The V1 contract is unchanged: `1.0.0` / `0.6.1` / 27 tools, with the gate
asserting that no DAG tool leaked into the projection.

### What the gate actually proves

The `DAG_ACCEPTED` gate is executable and fail-closed; a skipped test or a
missing criterion is a failure, not a pass. Beyond running the suite it performs
two live probes:

- **determinism** — two structurally identical plans that differ only in node
  order, `plan_id` and editorial text produce the same `plan_digest` and the
  same topological order;
- **durability** — a mutating node's write-ahead idempotency record is readable
  from the store *from inside the provider call*, and the terminal state
  survives a reload.

It further binds every Phase 5 module by SHA-256, AST-scans them for generic
surface (no shell, subprocess, socket, HTTP, `eval`/`exec`/`compile`), asserts
the closed TRANSFORM operation set has not drifted, and requires the
`BATCH_ACCEPTED` marker with `failures=[]` so Phase 4 provably preceded Phase 5.

### Decisions closed by this phase

| Open decision | Closed by | Outcome |
| --- | --- | --- |
| OD-003 durable store | ADR-0024 | SQLite WAL, stdlib only, `BEGIN IMMEDIATE` + monotonic fence token, integrity-digested records |
| OD-018 canonical serialization | ADR-0025 | Deterministic JSON reusing the accepted Phase 1 canonicalizer, prefixed with `digest_version` (plans; runbooks in Phase 6) |
| OD-021 replay format | ADR-0027 | Shaped node results, providers disabled, zero approval consumption, `replay=true` durable |
| OD-024 transform DSL | ADR-0026 | No DSL — a closed table of 12 pure typed bounded operations |

### Privacy controls

No plan document, checkpoint or evidence record retained for this phase contains
credential material. The store rejects secret-like fields before writing, and
A5-14 asserts the persisted body is free of `authorization`, `bearer`,
`client_secret`, `private_key` and `password`.

### Traceability

```text
Phase 5 requirements (docs/v2/requirements/traceability-matrix.md)
  -> docs/v2/phase5/*.md                                 [DESIGN]
  -> tests/fixtures/v2_phase5/plan_*.json                [FIXTURE CORPUS]
  -> tests/test_v2_phase5_dag_acceptance.py              [A5-01..A5-22, RUN]
  -> scripts/validate_v2_phase5_dag_gate.py              [INNER + OUTER]
  -> phase5-dag-acceptance.json                          [RETAINED]
  -> DAG_ACCEPTED                                        [DECLARED]
```

## Privacy controls

| Control | Phase 0 | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Phase 5 |
| --- | --- | --- | --- | --- | --- | --- |
| Prompt text retained | No | Not applicable / No | No | No | No | No |
| Output text retained | No | Not applicable / No | No | No | No | No |
| Raw credential values retained | No | No | No | No | No | No |
| Secret/environment paths retained | No | No | No | No | No | No |
| Free-text editorial metadata in canonical evidence | N/A | No | No | No | No | No (excluded from `plan_digest`) |
| Session ids / tool call ids / row contents retained | N/A | N/A | No | No | No | No |

## Reproducing validators

Phase 0:

```bash
python scripts/validate_v2_phase0_evidence.py \
  docs/v2/evidence/phase0-connected-baseline-20260808.json
```

Phase 1 validation is run in CI against the generated evidence for the exact
commit under test:

```bash
python scripts/v2_phase1_registry_acceptance.py \
  --source-commit <40-hex-sha> \
  --json-out phase1-registry-acceptance.json

python scripts/validate_v2_phase1_registry_evidence.py \
  phase1-registry-acceptance.json \
  --json-out phase1-registry-gate.json
```

Phase 2 outer acceptance is re-validated directly against the retained
document, which is self-contained:

```bash
python scripts/validate_v2_phase2_final_acceptance.py \
  docs/v2/evidence/phase2-final-evidence-20260809.json
```

It must print `overall_status: ACCEPTED` with `reasons: []` and exit `0`.

Phase 4 and Phase 5 gates are re-runnable directly against the working tree and
rewrite their own evidence document:

```bash
python scripts/validate_v2_phase4_batch_gate.py \
  --json-out docs/v2/evidence/phase4-batch-acceptance.json

python scripts/validate_v2_phase5_dag_gate.py \
  --json-out docs/v2/evidence/phase5-dag-acceptance.json
```

Each must print `"failures": []` with its accepted gate name and exit `0`.
