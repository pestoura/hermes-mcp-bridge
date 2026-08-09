# Runbook Manifest Contract (Normative Field Contract)

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> The concrete DSL/serialization is **OD-002 / OD-018 and remains open**. This
> document specifies the fields any chosen DSL must express and the rules
> admission enforces. The shape below is illustrative structure, not a chosen
> format. It does not supersede `../../contracts/runbook-example.md`, which
> remains the earlier conceptual sketch.

## Top-level fields

| Field | Required | Digest-relevant | Rule |
|---|---|---|---|
| `ir_schema_version` | yes | yes | Integer; unsupported → reject |
| `runbook_id` | yes | yes | `^RB-[A-Z0-9]+(-[A-Z0-9]+)+-[0-9]{3}$` |
| `version` | yes | yes | `MAJOR.MINOR.PATCH`, no pre-release/build |
| `title`, `description`, `rationale` | yes | **no** | Editorial; excluded from IR |
| `owner` | yes | yes (`owner.id`) | `{id, kind, contact}`; resolvable |
| `review_cadence_days` | yes | yes | Bounded integer |
| `policy_class` | yes | yes | `READ_ONLY` \| `MUTATING_LOW` \| `MUTATING_HIGH` \| `RESTRICTED` |
| `approval_class` | yes | yes | `NONE` \| `SINGLE` \| `DUAL` \| `OWNER_PLUS_SECURITY` |
| `destructive_action` | yes | yes | Boolean; computed and compared at admission |
| `accepted_irreversibility` | conditional | yes | Required when destructive and `rollback_support = NOT_SUPPORTED` |
| `rollback_support` | yes | yes | `NOT_APPLICABLE` \| `AUTOMATIC` \| `MANUAL` \| `NOT_SUPPORTED` |
| `timeout_ms` | yes | yes | Bounded by registry maximum |
| `budgets` | yes | yes | Defaults; agentic budgets 0 unless permitted |
| `requires_capabilities[]` | yes | yes | Must equal the computed set exactly |
| `credential_capability_ids[]` | yes | yes | Broker capability IDs only; never material |
| `resource_scope` | yes | yes | Scope expression, no unbounded wildcard for mutations |
| `min_capability_state` | yes | yes | Default `READY` |
| `parameters[]` | yes | yes | Closed typed schema (`../parameter-schema.md`) |
| `outputs` | yes | yes | Closed typed schema |
| `nodes[]` | yes | yes | See below |
| `edges[]` | yes | yes | Canonically sorted; acyclic |
| `tests[]` | yes | yes | Named tests that must pass against this digest |
| `created_at`, `last_reviewed_at` | registry | no | Bookkeeping |
| `runbook_digest` | computed | — | Never author-supplied |

## Node fields

| Field | Required | Rule |
|---|---|---|
| `key` | yes | Unique within the runbook, `^[a-z][a-z0-9_]{0,62}$` |
| `tool` | yes | `{name, version}` — exact pin, no floating reference |
| `inputs` | yes | Each binding is `param:<name>`, `node:<key>.<field>` or a literal |
| `policy_class` | yes | Node class; the runbook aggregate is `max()` over these |
| `destructive` | yes | Boolean; computed and compared |
| `node_timeout_ms` | yes | Critical path must fit `timeout_ms` |
| `retry_class` | yes | `NO_RETRY` \| `IDEMPOTENT_RETRY` \| `SAFE_READ_RETRY` |
| `idempotency` | conditional | Required for any mutating node with `IDEMPOTENT_RETRY` |
| `compensation` | conditional | Required when node `rollback_support = AUTOMATIC`; references a registered governed mutation |
| `expected_preconditions` | conditional | e.g. `expected_head_sha` for governed merge |
| `approval` | conditional | Node-level approval requirement (Phase 3 semantics), independent of runbook approval |

## Illustrative structure

```yaml
# ILLUSTRATIVE ONLY — the DSL is OD-002 and is NOT selected.
ir_schema_version: 1
runbook_id: RB-EXAMPLE-SUBJECT-001
version: 1.0.0
owner: { id: team/platform-security, kind: team, contact: "<channel>" }
review_cadence_days: 180
policy_class: MUTATING_LOW
approval_class: SINGLE
destructive_action: false
rollback_support: AUTOMATIC
timeout_ms: 600000
budgets:
  max_nodes: 8
  max_external_calls: 20
  max_parallelism: 2
  max_result_bytes: 262144
  max_retries: 2
  max_agentic_escalations: 0
  max_agentic_tokens: 0
requires_capabilities: [github.read, github.write.branch]
credential_capability_ids: [cap.github.read, cap.github.write.branch]
resource_scope: { github.repository: ["<org>/<explicit-repo>"] }
min_capability_state: READY
parameters:
  - { name: repository, type: resource_ref, resource_kind: github.repository,
      required: true, sensitivity: internal }
  - { name: branch_name, type: string, required: true, sensitivity: internal,
      constraints: { max_length: 100, pattern: "^[a-z0-9][a-z0-9._/-]{0,99}$" } }
outputs:
  branch_ref: { type: string }
nodes:
  - key: read_default_branch
    tool: { name: github.get_repository, version: "1.0.0" }
    inputs: { repository: "param:repository" }
    policy_class: READ_ONLY
    destructive: false
    node_timeout_ms: 30000
    retry_class: SAFE_READ_RETRY
  - key: create_branch
    tool: { name: github.create_branch, version: "1.0.0" }
    inputs:
      repository: "param:repository"
      branch_name: "param:branch_name"
      base_sha: "node:read_default_branch.default_branch_sha"
    policy_class: MUTATING_LOW
    destructive: false
    node_timeout_ms: 30000
    retry_class: IDEMPOTENT_RETRY
    idempotency: { key_fields: [repository, branch_name, base_sha] }
    compensation: { tool: { name: github.delete_branch_created_by_execution,
                            version: "1.0.0" } }
edges:
  - { from: read_default_branch, to: create_branch }
tests:
  - test_runbook_admission_example_ok
  - test_runbook_example_idempotent_repeat
  - test_runbook_example_compensation_residual_zero
```

## Prohibitions

- No credential material, tokens, headers or secrets in any field.
- No floating version references.
- No expression language, templating, shell, environment or filesystem lookup.
- No caller-supplied node overrides at invocation.
- No administrative capability references.
- No author-supplied `runbook_digest`.
