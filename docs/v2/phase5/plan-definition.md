# `PlanDefinition` — DAG Plan Schema (Design)

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

Normative shape for a Phase 5 plan. Supersedes the informal
`../contracts/dag-example.md` sketch. Field names are proposals; the digest
rules in `plan-digest.md` bind to whatever set is finally accepted.

## Top-level

```text
PlanDefinition
  schema_version        : str      required, exact match against supported set
  mode                  : "DAG"    required, literal
  plan_id               : str      caller-supplied stable id, opaque, bounded
  nodes                 : [Node]   required, 1..max_nodes, unique ids
  budget                : Budget   required, no defaults inherited silently
  failure_policy        : enum     fail_fast | continue_independent
  approval              : Approval optional, required if any node needs approval
  rollback_policy       : enum     none | compensate_on_failure | compensate_on_abort
  deadline_ms           : int      required, bounded by server maximum
  dry_run               : bool     default false; V2-FR-017 policy simulation
```

A plan is **data**. It carries no credentials, no endpoints, no URLs, no
commands. Anything not in this schema is rejected — unknown fields are a
validation failure, not ignored (fail-closed, and required for digest
determinism).

## Node

```text
Node
  id            : str            unique within plan, [a-z0-9_-]{1,64}
  kind          : enum           TOOL | TRANSFORM
  tool          : str            required iff kind == TOOL; must exist in the canonical registry
  op            : enum           required iff kind == TRANSFORM; closed set, see below
  args          : object         literal typed arguments, schema-validated against the tool definition
  bindings      : {path: Binding} typed value injection from upstream node results
  depends_on    : [node_id]      explicit edges; bindings imply edges but do not replace them
  policy_ref    : str            optional explicit policy selector; absence does not mean allow
  idempotency   : Idempotency    required for any node whose tool is classified mutating
  on_failure    : enum           abort_plan | isolate_branch | dead_letter
  timeout_ms    : int            bounded by plan deadline
  retry_ref     : str            optional named retry class (OD-011); never unbounded
  compensation  : Compensation   optional; only from the registry compensation table
```

### `kind: TOOL`

Executes exactly one canonical registry tool with exactly the arguments the
registry schema declares. No node may name a tool the caller's projection does
not expose (ADR-0005). The node inherits the tool's security tier, capability
requirement, credential capability id, quota class and audit class.

### `kind: TRANSFORM`

Deterministic, pure, in-process, no I/O, no credentials, no network, no clock,
no randomness. Closed operation set (OD-024 remains open on the exact list;
proposed minimum):

`select`, `project`, `filter_eq`, `filter_in`, `map_field`, `count`, `first`,
`sort_by`, `unique`, `merge_objects`, `to_list`, `require_non_empty`.

Each operation has a declared input type, output type and bounded output size.
Transforms cannot construct credentials, URLs, repository names outside the
already-authorized scope set, or arbitrary strings by concatenation of
attacker-controlled fragments without re-validation at the consuming node.

## Binding

```text
Binding
  from        : "<node_id>.result.<field_path>"   node must be in depends_on
  type        : declared type name from the V2 type table
  required    : bool
  max_bytes   : int
```

Rules:

1. The source node id must appear in `depends_on` of the consuming node.
2. `field_path` is a literal dotted path over the **shaped** result (the
   allow-listed projection), never over the raw provider payload.
3. The declared `type` must equal the source field's registry-declared type and
   the target argument's schema type. Mismatch = validation failure.
4. A bound value is re-validated against the target tool's argument schema
   before execution, exactly as if it were a literal.
5. Bound values never bypass scope enforcement. A binding that produces a
   repository outside the allow-list is denied at the consuming node, not
   silently accepted because an upstream node produced it.

## Budget

```text
Budget
  max_nodes              : int
  max_parallelism        : int
  max_external_calls     : int
  max_total_wall_ms      : int
  max_result_bytes       : int
  max_checkpoint_bytes   : int
  per_provider_limits    : {provider: int}      ASSUMPTION-P4-01
  per_credential_limits  : {capability_id: int} ASSUMPTION-P4-02
```

Budgets are ceilings, enforced by admission control before scheduling and again
per node dispatch. Exhaustion is a terminal plan status, never a silent trim.

## Approval

```text
Approval
  required_for : [node_id]
  digest       : plan_digest value the approval was issued against
  nonce        : single-use
  expires_at   : absolute
  scope        : the exact resource set the approval covers
```

Approval binds to the immutable `plan_digest` (V2-SEC-005). Any plan mutation
after approval invalidates it. See `plan-digest.md`.

## `rollback_policy`

| Value | Meaning |
|---|---|
| `none` | No compensation attempted. Partial effects remain and are reported explicitly. |
| `compensate_on_failure` | On a terminal failure with committed effects, run compensation in reverse topological order. |
| `compensate_on_abort` | Also compensate on caller cancellation or deadline abort. |

`rollback_policy` never permits an unsafe write. See `compensation-and-saga.md`.

## Requirements traced

V2-FR-004 (explicit dependencies), V2-FR-005 (typed schema-validated bindings),
V2-FR-016 (bounded deterministic transforms), V2-FR-017 (`dry_run`),
V2-SEC-012 (no arbitrary code), ADR-0003, ADR-0009, ADR-0011.
