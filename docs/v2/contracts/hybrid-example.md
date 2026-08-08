# HYBRID Contract Example

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

User intent: verify a PR and, only if it is failing, investigate why.

```text
DIRECT github.get_pr + github.get_checks
   |
   +-- GREEN -> return shaped result
   |
   +-- FAIL and objective.diagnose=true
          -> extract failing checks/log slices/commit/minimum diff
          -> AGENTIC escalation(reason=DIAGNOSIS_REQUIRED)
          -> return deterministic evidence + bounded diagnosis
```

The escalation request carries only minimum necessary context. Controls include allowed reason codes, `max_agentic_escalations`, token budget, timeout and context/result size limits. A failing deterministic operation does not automatically authorize a mutation or agentic action.
