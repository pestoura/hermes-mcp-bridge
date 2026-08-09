# DAG Validation and Cycle Detection (Design)

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

Validation is fully static and completes **before** any credential resolution,
any policy-approved dispatch and any external call. A plan that fails
validation produces zero broker calls and zero HTTP requests — the same
zero-side-effect property Phase 2 proves for out-of-scope reads.

## Fail-closed ordering

```text
1  schema/shape          reject unknown fields, bad types, size limits
2  identity              unique node ids; id charset; plan_id bounds
3  registry resolution   every TOOL node resolves to a canonical tool
4  projection            every tool is visible to this caller's projection
5  scope                 every literal resource argument is in the allow-list
6  graph structure       edges reference existing nodes; cycle detection; reachability
7  binding typing        source field exists, types equal, sizes bounded
8  transform typing      op in closed set; input/output types check
9  budget admission      node count, parallelism, external-call ceiling, deadline
10 policy (per node)     ALLOW / DENY / APPROVAL_REQUIRED for every node
11 approval binding      digest match, nonce unused, not expired, scope covers
12 idempotency planning  keys derivable for every mutating node
13 digest                compute plan_digest over the canonical form
--- only now may scheduling begin ---
```

Steps 1–9 are pure. Step 10 uses the Phase 1 policy engine unchanged. No step
short-circuits into execution on partial success.

## Cycle detection

Algorithm: Kahn topological sort over the edge set derived from
`depends_on` **unioned with** the edges implied by `bindings.from`.

```text
edges = { (src, dst) : dst.depends_on contains src }
      ∪ { (src, dst) : dst.bindings[*].from starts with src + "." }
```

If the union contains an edge not present in `depends_on`, that is a validation
error (`BINDING_EDGE_NOT_DECLARED`), not an implicit edge. Implicit dependency
creation is forbidden because it makes the digest and the review diff
misleading.

Outcomes:

| Condition | Reason code | Result |
|---|---|---|
| Kahn terminates with unprocessed nodes | `PLAN_CYCLE_DETECTED` | reject; report one minimal cycle deterministically (lexicographically smallest node set) |
| Self edge `n -> n` | `PLAN_SELF_DEPENDENCY` | reject |
| Edge to unknown node | `PLAN_UNKNOWN_DEPENDENCY` | reject |
| Duplicate edge | `PLAN_DUPLICATE_DEPENDENCY` | reject (digest determinism) |
| Node unreachable from any root and produces no consumed output | `PLAN_UNREACHABLE_NODE` | reject (dead nodes hide intent) |
| Graph depth > `max_depth` | `PLAN_DEPTH_EXCEEDED` | reject |
| Fan-out from one node > `max_fanout` | `PLAN_FANOUT_EXCEEDED` | reject |

Cycle reporting must be deterministic so the same bad plan always yields the
same error body — an error body is part of the auditable contract.

## Binding validation

For each binding on node `d` with `from = "s.result.a.b"`:

1. `s` ∈ `d.depends_on` else `BINDING_EDGE_NOT_DECLARED`.
2. `s.kind` produces a declared result schema (registry tool result allow-list,
   or transform output type) else `BINDING_SOURCE_UNSHAPED`.
3. `a.b` exists in that declared schema else `BINDING_FIELD_UNKNOWN`. Paths are
   resolved against the **schema**, statically — never against runtime data.
4. Declared `type` equals the source type else `BINDING_TYPE_MISMATCH`.
5. Declared `type` is accepted by the target argument slot else
   `BINDING_TARGET_TYPE_MISMATCH`.
6. `max_bytes` ≤ the target slot's schema bound else `BINDING_SIZE_EXCEEDED`.
7. Target path must be an argument slot, never `tool`, `policy_ref`,
   `idempotency`, `compensation` or any control field —
   `BINDING_CONTROL_FIELD_FORBIDDEN`. A plan may not compute what it executes.

Rule 7 is the central injection control: data can flow into arguments, never
into the choice of operation, credential or scope.

## Runtime re-validation

Static typing is necessary but not sufficient — a provider can return a
schema-conformant but hostile value. At dispatch time, every bound value is:

- re-validated against the consuming tool's argument schema;
- re-checked against the resource allow-list if it names a resource;
- length/charset-checked against the slot's declared constraints;
- never used to construct a URL path segment without the provider layer's own
  encoding and allow-list checks (inherited from the Phase 2 DIRECT read core).

Failure at re-validation is a node failure with reason `BINDING_RUNTIME_REJECT`
and is **not** retried, because the input is deterministic.

## `dry_run`

`dry_run: true` executes steps 1–13 and then reports, per node: resolved tool,
policy decision, credential capability id and readiness, idempotency key shape
(not the key material), scheduling wave, and the computed `plan_digest`. It
performs zero credential resolution beyond readiness status and zero external
calls. `dry_run` output is not an approval and cannot be replayed as one.

## Requirements traced

V2-FR-004, V2-FR-005, V2-FR-017, V2-FR-019, V2-SEC-003, V2-SEC-012, ADR-0009.
