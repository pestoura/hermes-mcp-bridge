# Exemplar — `RB-GITHUB-PR-LIFECYCLE-001`

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Illustrative. This manifest is **not admitted**, not registered, not
> executable, and its presence in the repository grants nothing. It expands the
> conceptual sketch in `../../contracts/runbook-example.md` into the field
> contract of `runbook-manifest-contract.md`.

## Scope decision

The exemplar covers the **non-merge** portion of the PR lifecycle:

```
read_repo -> create_branch -> create_pr -> read_checks -> evaluate_checks
```

Merge is deliberately **excluded** from version `1.0.0`. Governed merge stays a
separate, later increment under ADR-0023 (explicit merge-enabled repository
list, distinct human approver, live branch protection verification,
`expected_head_sha`, `NO_RETRY`, no automatic compensation). Bundling merge into
the first exemplar would make the first runbook the highest-risk one.

There is no agentic diagnosis node: `max_agentic_escalations = 0`. The
diagnose-on-failure idea from the earlier sketch is deferred to Phase 8 and is
not admissible before `HYBRID_ACCEPTED`.

## Manifest (illustrative)

```yaml
# ILLUSTRATIVE ONLY — DSL is OD-002 and NOT selected. NOT ADMITTED.
ir_schema_version: 1
runbook_id: RB-GITHUB-PR-LIFECYCLE-001
version: 1.0.0
title: GitHub PR lifecycle (branch + PR + check evaluation)
owner: { id: team/platform-security, kind: team, contact: "<operational channel>" }
review_cadence_days: 180
policy_class: MUTATING_LOW
approval_class: SINGLE
destructive_action: false
rollback_support: AUTOMATIC
timeout_ms: 900000
budgets:
  max_nodes: 8
  max_external_calls: 40
  max_parallelism: 2
  max_runtime_ms: 900000
  max_result_bytes: 262144
  max_artifacts: 4
  max_retries: 2
  max_agentic_escalations: 0
  max_agentic_tokens: 0
requires_capabilities:
  - github.read
  - github.write.branch
  - github.write.pull_request
credential_capability_ids:
  - cap.github.read
  - cap.github.write.branch
  - cap.github.write.pull_request
resource_scope:
  github.repository: ["<org>/<explicitly-enrolled-repo>"]
min_capability_state: READY

parameters:
  - name: repository
    type: resource_ref
    resource_kind: github.repository
    required: true
    sensitivity: internal
  - name: branch_name
    type: string
    required: true
    sensitivity: internal
    constraints: { max_length: 100, pattern: "^[a-z0-9][a-z0-9._/-]{0,99}$" }
  - name: base_ref
    type: string
    required: false
    default: "<repository default branch>"
    sensitivity: internal
    constraints: { max_length: 100, pattern: "^[a-z0-9][a-z0-9._/-]{0,99}$" }
  - name: pr_title
    type: string
    required: true
    sensitivity: internal
    constraints: { max_length: 200 }
  - name: pr_body
    type: string
    required: false
    default: ""
    sensitivity: internal
    constraints: { max_length: 8000 }
  - name: wait_checks_ms
    type: integer
    required: false
    default: 300000
    constraints: { min: 0, max: 600000 }

outputs:
  branch_ref: { type: string }
  pull_request_number: { type: integer }
  pull_request_url: { type: string }
  checks_state: { type: enum, enum_values: [GREEN, FAILED, PENDING, UNKNOWN] }

nodes:
  - key: read_repo
    tool: { name: github.get_repository, version: "1.0.0" }
    inputs: { repository: "param:repository" }
    policy_class: READ_ONLY
    destructive: false
    node_timeout_ms: 30000
    retry_class: SAFE_READ_RETRY

  - key: resolve_base
    tool: { name: github.get_ref, version: "1.0.0" }
    inputs:
      repository: "param:repository"
      ref: "param:base_ref"
    policy_class: READ_ONLY
    destructive: false
    node_timeout_ms: 30000
    retry_class: SAFE_READ_RETRY

  - key: create_branch
    tool: { name: github.create_branch, version: "1.0.0" }
    inputs:
      repository: "param:repository"
      branch_name: "param:branch_name"
      base_sha: "node:resolve_base.sha"
    policy_class: MUTATING_LOW
    destructive: false
    node_timeout_ms: 30000
    retry_class: IDEMPOTENT_RETRY
    idempotency: { key_fields: [repository, branch_name, base_sha] }
    compensation:
      tool: { name: github.delete_branch_created_by_execution, version: "1.0.0" }
      note: "Deletes only a branch this execution created, proven by the
             write-ahead audit record; never a pre-existing branch."

  - key: create_pr
    tool: { name: github.create_pr, version: "1.0.0" }
    inputs:
      repository: "param:repository"
      head: "node:create_branch.branch_ref"
      base: "node:resolve_base.ref"
      title: "param:pr_title"
      body: "param:pr_body"
    policy_class: MUTATING_LOW
    destructive: false
    node_timeout_ms: 60000
    retry_class: IDEMPOTENT_RETRY
    idempotency: { key_fields: [repository, head, base, title] }
    compensation:
      tool: { name: github.close_pr_created_by_execution, version: "1.0.0" }

  - key: read_checks
    tool: { name: github.get_check_runs, version: "1.0.0" }
    inputs:
      repository: "param:repository"
      ref: "node:create_branch.branch_ref"
      wait_ms: "param:wait_checks_ms"
    policy_class: READ_ONLY
    destructive: false
    node_timeout_ms: 600000
    retry_class: SAFE_READ_RETRY

  - key: evaluate_checks
    tool: { name: github.evaluate_check_state, version: "1.0.0" }
    inputs: { check_runs: "node:read_checks.check_runs" }
    policy_class: READ_ONLY
    destructive: false
    node_timeout_ms: 10000
    retry_class: SAFE_READ_RETRY

edges:
  - { from: read_repo,      to: resolve_base }
  - { from: resolve_base,   to: create_branch }
  - { from: create_branch,  to: create_pr }
  - { from: create_pr,      to: read_checks }
  - { from: read_checks,    to: evaluate_checks }

tests:
  - test_runbook_admission_pr_lifecycle_ok
  - test_runbook_pr_lifecycle_capability_exact_match
  - test_runbook_pr_lifecycle_idempotent_repeat_single_mutation
  - test_runbook_pr_lifecycle_out_of_scope_repo_zero_http
  - test_runbook_pr_lifecycle_approval_digest_mismatch_denies
  - test_runbook_pr_lifecycle_compensation_residual_zero
  - test_runbook_pr_lifecycle_zero_llm_tokens
```

## Notes on the exemplar

- `checks_state = FAILED` is a **normal, successful** runbook outcome: the
  runbook reports state, it does not decide what to do about it. No branch is
  created twice and no PR is closed automatically on a red build.
- Compensation deletes/closes only objects this execution created, established
  from the write-ahead audit records — never inferred from names.
- `resolve_base` exists so that `base_sha` is an explicit, digest-bound value
  rather than a value the provider picks at mutation time; this is what makes
  precondition drift detectable.
- Every mutating node is `MUTATING_LOW` and non-destructive, so
  `approval_class: SINGLE` is permitted. Adding merge would force
  `MUTATING_HIGH` and `DUAL`, and is therefore a different runbook version or a
  different runbook.
- Admission would still recompute the capability set, the destructive marker and
  the policy aggregate and compare them with the declarations above. Nothing
  here is trusted because it is written down.
