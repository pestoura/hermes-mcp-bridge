# Canonical Tool Registry

> **V2 · PHASE 1 CORE IMPLEMENTED · NOT YET ACCEPTED · NO IMPACT ON V1**

Phase 1 implements this document as an **isolated, in-process typed package**
at `src/hermes_mcp_bridge/v2/`. Nothing in that package is imported by the V1
server or tool registration path, and the V1 27-tool surface is unchanged.
`REGISTRY_ACCEPTED` is **not** declared.

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

## Phase 1 implementation notes

Implemented (`hermes_mcp_bridge.v2`):

- `ToolDefinition` (`schema.py`) — canonical typed definition carrying
  `tool_id`, `provider`, `operation`, `execution_mode`, `input_schema`,
  `output_schema`, `security_tier`, `read_only`, `mutation_class`,
  `idempotency`, `credential_capability_id`, `policy_action`,
  `approval_requirement`, `timeout_seconds`, `retry_policy`, `resource_key`,
  `result_shaping`, `capability_id`, `version`, plus non-secret
  `backend`/`stability`/`deprecated` metadata and free-text `description`.
  `description` is editorial metadata only: it is **non-canonical and
  non-projected** in Phase 1 (excluded from `canonical()`, from the capability
  snapshot hash and from the client projection), because free text cannot be
  secret-scanned with confidence. `canonical()` is the only audit-safe
  serialization; `model_dump()` is not.
- Enforced invariants: identifiers are non-empty, normalized (trimmed,
  lowercase, dotted `[a-z0-9_-]`) and wildcard-free; `read_only` implies
  `mutation_class = NONE`, tier T0/T1 and `idempotency = READ`; mutating tools
  are T2+ with a non-`NONE` mutation class; `DESTRUCTIVE` <-> `T4` is
  bidirectional; `timeout_seconds` is bounded to `[1, 3600]`; schemas must be
  non-empty JSON objects of `type: object`; `tool_id` must be namespaced by
  `provider`; duplicate `tool_id`/`capability_id` are rejected; references to
  unknown capabilities or credential capabilities are rejected; non-idempotent
  mutations may not declare `RETRY_SAFE`.
- `CapabilityRegistry` / `ToolRegistry` (`capabilities.py`, `registry.py`) —
  deterministic, duplicate-checked, controlled mutation until `freeze()`, and
  fail-closed lookups (`UnknownToolError` / `UnknownCapabilityError`; an
  unknown capability is never "ready").
- `CapabilityState` (`enums.py`) distinguishes `CONFIGURED`, `AVAILABLE`,
  `HEALTHY`, `READY`, `DEGRADED`, `UNAVAILABLE`, `DENIED` with unambiguous
  `is_configured` / `is_available` / `is_healthy` / `is_ready` properties.
  `DENIED` is the ADR-0004 `UNAUTHORIZED` state; `DEGRADED` is available but
  not healthy. Only `READY` may execute.
- `capability_snapshot_hash` — canonical JSON (UTF-8, sorted keys, separators
  `(",", ":")`, no floats, tools ordered by `tool_id`, capabilities by
  `capability_id`, no timestamps or paths) digested with SHA-256 into a
  lowercase 64-hex string. Insertion order cannot change it; any material
  change (tool set, schema, state, version, tier, policy action, timeout) does.
  The snapshot carries **non-secret metadata only**.

Deferred (unchanged): registry persistence, storage format and signing — open
questions of ADR-0004, with no OD entry of their own; schema migration process;
dynamic discovery/refresh (OD-012).
