# V2 Phase 0 — Acceptance Evidence Index

> **Gate:** `BASELINE_ACCEPTED` · **Date:** 2026-08-08 · **No V1 semantic change**

This directory holds the sanitized acceptance evidence for V2 Phase 0. It contains
aggregates, hashes and numeric metrics only. No prompt text, no output text, no
secrets, no authentication tokens, no credential or environment paths.

## Runtime under test

| Item | Value |
| --- | --- |
| Bridge version | `1.0.0` |
| Schema version | `0.6.1` |
| Upstream Hermes status | `ok` |
| Base commit (`main`) | `f0b7e72f6bdf42e82712f3d2e8182ff937ae9509` |
| Collection window | 2026-08-08T04:07:42Z → 2026-08-08T04:09:38Z |

## Evidence files and digests

| File | SHA-256 |
| --- | --- |
| `phase0-connected-baseline-20260808.json` | `ea6cc080891d133fa835a4e852f69dd124feaeccc38befe24293395020667559` |
| `phase0-connected-baseline-gate-20260808.json` | `8db24e8436fcac4b2a9eae2f41ad5b8e9c5194a0f174a3b0f3f67e612e0f9997` |

`phase0-scenarios.example.json` remains a non-evidence scenario template.

## Result summary

Three required categories, three repetitions each (9/9 successful, 0 failures,
0 contaminated metric windows, `bridge_execution_terminal_total` delta exactly `1`
in every repetition).

| Category | Scenario ID | Runs | Successes | Tokens (total) |
| --- | --- | --- | --- | --- |
| `read` | `github_read_repo_metadata` | 3 | 3 | 181,063 |
| `mutation` | `local_disposable_roundtrip` | 3 | 3 | 246,111 |
| `agentic` | `bounded_execution_mode_reasoning` | 3 | 3 | 91,874 |
| **Total** | — | **9** | **9** | **519,048** |

Token counts are real accounting from the Hermes execution result path, not
character-based estimates.

## Privacy and cleanup

| Control | Result |
| --- | --- |
| `prompts_stored` | `false` — PASS |
| `outputs_stored` | `false` — PASS |
| `secrets_stored` | `false` — PASS |
| MCP URL scope | `loopback` |
| Hermes state DB access | enabled, read-only |
| Mutation cleanup residual count | `0` — PASS |

Prompts are represented only by `prompt_sha256`; outputs only by `output_bytes`.

## Traceability chain

```text
requirements (docs/v2/requirements/)
        ↓
harness (scripts/v2_phase0_benchmark.py)
        ↓
evidence (docs/v2/evidence/phase0-connected-baseline-20260808.json)
        ↓
validator (scripts/validate_v2_phase0_evidence.py, fail-closed)
        ↓
gate (docs/v2/evidence/phase0-connected-baseline-gate-20260808.json → BASELINE_ACCEPTED)
```

Contract tests for this chain live in `tests/test_v2_phase0_evidence.py`.
Gate semantics are documented in `docs/v2/phase0-benchmark.md`.

## Reproducing the validation

```bash
python scripts/validate_v2_phase0_evidence.py \
  docs/v2/evidence/phase0-connected-baseline-20260808.json
```

Expected output: `{"failures": [], "gate": "BASELINE_ACCEPTED"}`.
