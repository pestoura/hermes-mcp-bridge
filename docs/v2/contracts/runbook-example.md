# RUNBOOK Contract Example — RB-GITHUB-PR-LIFECYCLE-001

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

Conceptual known workflow:

```text
preflight
  -> create_branch
  -> apply_changes
  -> validate
  -> commit
  -> push
  -> create_pr
  -> wait_checks
  -> evaluate_checks
       | GREEN -> policy/approval -> merge -> verify_main
       | FAIL  -> stop
                    \-> optional agentic diagnosis only when diagnose=true
  -> signed result manifest
```

Runbook metadata should include immutable version/digest, required tool versions/capabilities, credential capability IDs, security tier, parameter schema, resource scopes, budget defaults, idempotency/lock requirements, compensation declarations and tests.

Promotion path: stable skill/procedure -> reviewed runbook definition -> schema/threat/tests -> compile/canonical digest -> optional signing -> staged promotion.
