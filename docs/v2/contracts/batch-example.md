# BATCH Contract Example

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

User intent: search X in GitHub and validate Y in email with one bridge request.

```json
{
  "schema_version": "v2-draft",
  "mode": "BATCH",
  "operations": [
    {"id": "github_search", "tool": "github.search", "args": {"query": "X"}, "select": ["count", "top5"]},
    {"id": "email_search", "tool": "email.search", "args": {"query": "Y"}, "select": ["count", "latest"]}
  ],
  "failure_policy": "continue_on_error",
  "budget": {"max_nodes": 2, "max_parallelism": 2, "max_external_calls": 4}
}
```

Conceptual response:

```json
{
  "status": "completed",
  "aggregate_status": "SUCCESS",
  "operations": {
    "github_search": {"status": "SUCCESS", "result": {}},
    "email_search": {"status": "SUCCESS", "result": {}}
  }
}
```

Each operation has independent policy, scope, quota, risk, credential, audit and shaping. One request does not mean one authorization decision.
