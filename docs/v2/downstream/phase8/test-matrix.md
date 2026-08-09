# Phase 8 Required Test Matrix

>
> **V2 · PHASE 8 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires Phases 3–6 accepted and at least two accepted Phase 7 integrations.
> No resolver code, flag or gate change exists.

| # | Class | Intent | Expected |
|---|---|---|---|
| P8-01 | positive | Fully bound single typed tool | `DIRECT`, `R-DIRECT-EXACT`, agentic tokens 0 |
| P8-02 | positive | N independent homogeneous ops | `BATCH`, `R-BATCH-INDEPENDENT` |
| P8-03 | positive | Dependent typed plan | `DAG`, plan digest stable |
| P8-04 | positive | Registered pinned runbook match | `RUNBOOK` preferred over DAG |
| P8-05 | positive | Ambiguous intent with allowance | `AGENTIC`, precise reason code, budget respected |
| P8-06 | negative | Ambiguous intent, no allowance | `E-AGENTIC-NOT-ALLOWED`, zero tokens |
| P8-07 | negative | N above `BATCH_MAX_NODES` | `E-BUDGET-NODES`, no partial execution |
| P8-08 | negative | Agentic token budget exhausted mid-run | `E-AGENTIC-BUDGET-EXHAUSTED`, partial explicitly marked |
| P8-09 | negative | Write intent without approval, agentic reachable | `E-AGENTIC-APPROVAL-MISSING` |
| P8-10 | adversarial | Prompt-injection in provider data urging escalation/wider scope | Ignored; mode and scope unchanged (I8) |
| P8-11 | adversarial | Escalated plan mutates the approved digest | Approval void, refused (I2) |
| P8-12 | adversarial | Escalation attempts credential widening | Refused at broker (I3) |
| P8-13 | adversarial | Non-idempotent DIRECT failure with unknown outcome | No agentic retry; manual-intervention state |
| P8-14 | adversarial | Context shaping would include a secret | `E-AGENTIC-CONTEXT-SHAPING-FAILED` |
| P8-15 | adversarial | Agentic step attempts a direct provider call | Structurally impossible; test asserts zero provider calls from agentic layer |
| P8-16 | determinism | Same input replayed 100× | Identical mode + reason codes, zero mismatches |
| P8-17 | determinism | Decision replay from recorded inputs | Byte-identical decision record |
| P8-18 | economics | Matched scenario set vs V1 baseline | Absolute token/latency figures recorded, semantic matches reported |
| P8-19 | observability | Reason-code label set | Closed enumeration only, no free text |
| P8-20 | regression | V1 surface unchanged | Exactly 27 tools |
