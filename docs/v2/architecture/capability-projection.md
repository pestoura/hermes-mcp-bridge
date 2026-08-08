# Capability Projection

> **V2 · PHASE 1 CORE IMPLEMENTED · NOT YET ACCEPTED · NO IMPACT ON V1**

Phase 1 implements a **static, deterministic** projection in
`hermes_mcp_bridge.v2.projection`. `REGISTRY_ACCEPTED` is not declared.

Hermes may contain hundreds of internal tools and skills. The external client must receive only a policy-approved subset.

```text
Hermes Internal Registry
        |
        v
policy / allowlist / principal / scope
        |
        v
Projected Tool Surface
        |
        v
ChatGPT / MCP client
```

Projection must exclude secrets, credential values, secret paths and unrestricted dangerous capabilities. Schemas should include only fields required by the client contract.

## Projection inputs

- principal/tenant context;
- resource scope;
- active policy version;
- tool security tier;
- credential capability availability (not secret values);
- capability health;
- environment/stability constraints;
- protocol/schema negotiation.

## Open design choice

Static projection is simpler and auditable; dynamic projection can reduce context/tool count further. V2 must document and test whichever model is selected and must avoid metadata supplied by an untrusted backend silently expanding authority.

**Phase 1 decision:** static projection. `project_capabilities(registry, rules,
credential_broker, context)` is a pure function of its inputs and produces a
list ordered by `tool_id` plus a `projection_hash` and the
`capability_snapshot_hash` it was computed against. Dynamic projection remains
deferred (OD-013), as does the discovery/refresh protocol (OD-012).

## Phase 1 projection rules

- only `ALLOW` and `APPROVAL_REQUIRED` decisions are projected;
  `APPROVAL_REQUIRED` is always carried with an explicit `requires_approval`
  flag and is never presented as a silent `ALLOW`;
- `DENY`, unknown tools, non-`READY` capabilities and unavailable credential
  capabilities are excluded, each with a stable reason code retained in
  `ProjectionResult.excluded` for audit;
- the projected payload is a strict field allow-list: `tool_id`, `provider`,
  `operation`, `version`, `execution_mode`, `input_schema`, `output_schema`,
  `security_tier`, `read_only`, `mutation_class`, `result_shaping`,
  `timeout_seconds`, `requires_approval`, `description`. It never contains
  credential values, `credential_capability_id`, `capability_id`, secret paths,
  backend identifiers, terminal or filesystem capabilities, or unnormalized
  backend-supplied metadata;
- the caller context (`ProjectionContext`) is **opaque and minimal**
  (`principal_ref`, `resource_scope_ref`). It participates in no Phase 1
  authorization decision and is deliberately not serialized, so the
  principal/tenant model (OD-007) stays open.

## Internal MCP proxying

An internal MCP server such as Home Assistant may be projected through normalized typed tools, but internal MCP metadata is not trusted automatically. Tool names, schemas, risk classification and policy actions must be normalized and independently governed by the canonical registry.
