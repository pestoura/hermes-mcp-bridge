# Phase 5 non-runtime plan fixtures

> **V2 - PHASE 5 - DESIGN ONLY - NOT IMPLEMENTED - NO V1 IMPACT**

Inert JSON documents only. No Python, no imports, no conftest hooks, no
collection by pytest. Nothing in Phase 3 or Phase 4 reads this directory, so
these files cannot conflict with existing code paths.

They exist so the Phase 5 validation/digest suites (see
`docs/v2/phase5/test-plan.md`) have a fixed, reviewable corpus once
`BATCH_ACCEPTED` is declared, and so the design can be reviewed against
concrete shapes now.

`expected_reason_code` is a review annotation for negative fixtures, not part
of the `PlanDefinition` schema; the future loader strips it.
