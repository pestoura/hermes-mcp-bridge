# Canonical Tool Registry

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

The Tool Registry normalizes capabilities from Hermes native tools, typed CLI wrappers, APIs, plugins, internal MCP servers and future connectors.

## Minimum canonical metadata

`canonical_name`, `version`, `description`, `input_schema`, `output_schema`, `read_only`, `mutation_class`, `risk_class`, `policy_action`, `required_resource_scope`, `credential_capability`, `idempotency_semantics`, `timeout`, `retry_class`, `concurrency_hint`, `lock_hint`, `backend`, `provenance`, `result_shaping`, `cost_hint`, `stability`, `deprecated`, `version_added`.

Additional v2 metadata should support: `latency_class`, `cost_class`, `rate_limit_class`, `llm_required`, `security_tier`, capability health and secret-aware field classifications.

## Example

```yaml
canonical_name: github.get_pr
version: 1
backend: github-api
read_only: true
mutation_class: none
risk_class: low
security_tier: T0
policy_action: github.pr.read
required_resource_scope: repository
credential_capability: github.read
idempotency_semantics: read
retry_class: RETRY_SAFE
result_shaping: supported
llm_required: false
```

## Security tiers

- T0 — read-only harmless;
- T1 — read-only sensitive;
- T2 — low-risk mutation;
- T3 — privileged mutation;
- T4 — destructive/admin.

## Capability health

Registration does not imply usability. Registry/health state must distinguish `AVAILABLE`, `DEGRADED`, `UNAVAILABLE`, `UNAUTHORIZED`.

## Capability snapshots

Each execution should be able to record `capability_manifest_hash` so audit/replay can identify the exact tool surface and versions used.
