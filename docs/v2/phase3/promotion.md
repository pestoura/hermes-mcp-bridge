# Phase 3 promotion — `DIRECT_MUTATION_ACCEPTED`

Machine-checked promotion of Phase 3 (GitHub DIRECT mutations).

## Gate runner

```
python scripts/validate_v2_phase3_direct_mutation_gate.py \
    --json-out docs/v2/evidence/phase3-direct-mutation-acceptance.json
```

Exit code `0` only when `failures == []`. The script has no self-approval path:
each criterion in `docs/v2/phase3/acceptance-criteria.md` is either evaluated
against real repository/runtime state or recorded as a failure.

* INNER — V1 contract invariants (contract `1.0.0`, schema `0.6.1`, 27 tools),
  Phase 3 preflight, real lane tests (governed merge, merge gates, merge
  executor, mutation registry), runtime destructive-exclusion assertion,
  A3-13 token-accounting probe, A3-14 result-schema secret scan.
* OUTER — SHA-256 binding of every Phase 3 source module plus the A3-01 check
  that `DIRECT_READ_ACCEPTED` preceded Phase 3.

No credential material is read by the gate; the accounting probe opens the
runtime state DB read-only and uses aggregate counts only.

## Recorded result

`docs/v2/evidence/phase3-direct-mutation-acceptance.json`

| field | value |
| --- | --- |
| `gate` | `DIRECT_MUTATION_ACCEPTED` |
| `failures` | `[]` |
| `source_commit` | `8fc8363a3eb31db99c18afb39fcd78bde011e2b6` |

V1 remains unchanged by this promotion: contract `1.0.0`, schema `0.6.1`,
27 tools, HMAC policy fail-closed.
