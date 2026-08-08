# DIRECT Contract Example

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

Conceptual contract only; final field names/versioning are subject to ADR acceptance.

```json
{
  "schema_version": "v2-draft",
  "mode": "DIRECT",
  "operation": {
    "id": "pr",
    "tool": "github.get_pr",
    "args": {"repository": "owner/project", "number": 123},
    "select": ["number", "title", "state", "author", "head_sha"]
  },
  "budget": {"max_external_calls": 1, "max_result_bytes": 16384}
}
```

Execution path: schema -> policy -> scope -> credential capability `github.read` -> tool backend -> result shaping -> provenance/manifest. No Hermes LLM is invoked.
