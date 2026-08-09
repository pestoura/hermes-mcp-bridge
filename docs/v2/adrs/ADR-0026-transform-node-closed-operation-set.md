# ADR-0026 — TRANSFORM Nodes Are a Closed Operation Set (closes OD-024)

> **V2 · PHASE 5 · IMPLEMENTED BEHIND `DAG_FEATURE_ENABLED` · NO V1 IMPACT**

**Status:** Accepted (Phase 5)

## Context
OD-024 asked what a TRANSFORM node may compute. A DAG needs to reshape one
node's output into another node's input (select a field, filter a list, count
results). The tempting answer is a small expression language — JMESPath, JSONata,
a template engine, or a sandboxed `eval`. ADR-0003 forbids generic shell exposure
for the same reason a mini-language is dangerous: it turns a data document into
a program, and the plan document is attacker-influenced input.

## Decision
TRANSFORM nodes select from a **closed, code-defined table of pure operations**
(`v2/dag_transform.py`): `select`, `project`, `filter_eq`, `filter_in`,
`map_field`, `count`, `first`, `sort_by`, `unique`, `merge_objects`, `to_list`,
`require_non_empty`. There is no expression string, no template, no user-supplied
predicate and no dynamic dispatch by name onto arbitrary attributes. Each entry
declares exact arity and argument types; unknown ops are `TRANSFORM_OP_UNKNOWN`
and arity/type breaches are `TRANSFORM_TYPE_MISMATCH`, both at static validation
time. Every operation is total, side-effect free, non-recursive and bounded: the
canonical encoding of its output is size-checked against the plan budget
(`TRANSFORM_OUTPUT_TOO_LARGE`). Adding an operation is a reviewable code change,
not a runtime capability.

## Consequences
Some legitimate reshaping is not expressible and must be done by adding a
reviewed operation. In exchange, a TRANSFORM node cannot loop, cannot recurse,
cannot allocate unboundedly, cannot reach a provider and cannot execute
attacker-authored logic. A5-03 enforces this structurally with an AST scan of
every Phase 5 module for `eval`/`exec`/`compile`/`__import__` and for
shell/socket/HTTP imports.

## Alternatives
* **JMESPath / JSONata / CEL** — rejected: a full expression evaluator over
  untrusted input, with its own CVE surface and its own resource-exhaustion
  semantics, for a handful of reshaping needs.
* **Jinja-style templates** — rejected: string templating over provider output is
  an injection sink and produces untyped results.
* **Sandboxed `eval`** — rejected outright; sandboxing Python expressions is a
  losing position and directly contradicts ADR-0003.

## Security implications
Removes arbitrary computation from the plan document. Combined with runtime
re-validation of bound values (type, size and — for resource arguments — scope),
a hostile provider response cannot widen scope or smuggle logic through a
transform.

## Operational implications
Requests for new operations arrive as pull requests with tests. The operation
table is part of the gate's OUTER layer: drift in the set is a gate failure.

## Open questions
Whether to add typed numeric aggregation (`sum`, `max`) in a later phase; it was
deliberately left out of Phase 5 as unnecessary.
