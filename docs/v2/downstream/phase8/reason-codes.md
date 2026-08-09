# Enumerated Reason Codes

>
> **V2 · PHASE 8 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires Phases 3–6 accepted and at least two accepted Phase 7 integrations.
> No resolver code, flag or gate change exists.

Codes are stable, closed and safe as bounded metric labels.

## Mode selection (`R-`)

| Code | Meaning |
|---|---|
| `R-DIRECT-EXACT` | Exactly one registered typed tool, fully bound, capability ready |
| `R-RUNBOOK-MATCH` | Version-pinned runbook matched with bound inputs |
| `R-BATCH-INDEPENDENT` | N>1 homogeneous independent typed operations |
| `R-DAG-TYPED-PLAN` | Typed dependency plan with computable digest |
| `R-AGENTIC-AMBIGUOUS-INTENT` | Intent not expressible in any typed contract |
| `R-AGENTIC-UNBOUND-ARGUMENT` | Required argument cannot be resolved deterministically |
| `R-AGENTIC-UNKNOWN-TARGET` | Target set must be inferred, not enumerated |
| `R-AGENTIC-NO-CONTRACT-COVERAGE` | Required operation has no registered tool/runbook |
| `R-AGENTIC-RESIDUAL-SUBINTENT` | Deterministic segment succeeded; remainder needs reasoning |

## Rejected-branch records (`R-REJ-`)

`R-REJ-DIRECT-NOT-READY`, `R-REJ-DIRECT-MULTI-TOOL`, `R-REJ-DIRECT-UNBOUND`,
`R-REJ-NO-RUNBOOK`, `R-REJ-RUNBOOK-VERSION-UNPINNED`, `R-REJ-NOT-INDEPENDENT`,
`R-REJ-NOT-PLANNABLE`, `R-REJ-CYCLE-DETECTED`.

## Refusals (`E-`)

| Code | Meaning |
|---|---|
| `E-REQ-INVALID` | Typed schema validation failed |
| `E-POLICY-DENY` | Policy denied the intent |
| `E-BUDGET-NODES` / `E-BUDGET-TOKENS` / `E-BUDGET-DEADLINE` | Declared budget exceeded |
| `E-AGENTIC-NOT-ALLOWED` | No agentic allowance declared |
| `E-AGENTIC-BUDGET-EXHAUSTED` | Escalation/token/time budget spent |
| `E-AGENTIC-APPROVAL-MISSING` | Write/T3 intent without approval binding |
| `E-AGENTIC-POLICY-FORBIDDEN` | Policy forbids agentic for this scope |
| `E-AGENTIC-CONTEXT-SHAPING-FAILED` | Minimum-context shaping could not exclude sensitive material |
| `E-SAFETY-DOWNGRADE-REFUSED` | The only reachable path would weaken a safety control |
| `E-RESOLVER-NONDETERMINISM` | Replay produced a different decision — hard failure |

Every terminal outcome carries exactly one primary code plus the ordered
rejected-branch list.
