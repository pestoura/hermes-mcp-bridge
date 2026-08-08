# V2 Acceptance Evidence Index

> **Phase 0:** `BASELINE_ACCEPTED` · **Phase 1:** `REGISTRY_ACCEPTED` · **Date:** 2026-08-08 · **V1 semantics preserved**

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

## Privacy controls

| Control | Phase 0 | Phase 1 |
| --- | --- | --- |
| Prompt text retained | No | Not applicable / No |
| Output text retained | No | Not applicable / No |
| Raw credential values retained | No | No |
| Secret/environment paths retained | No | No |
| Free-text editorial metadata in canonical evidence | N/A | No |

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
