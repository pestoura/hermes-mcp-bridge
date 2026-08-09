# Runbook Parameter and Output Schema

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

## Principles

1. Every runbook declares a **closed** typed parameter schema and a **closed**
   typed output schema. Unknown properties are rejected, never ignored.
2. Validation happens before any capability resolution, credential resolution
   or provider call (see `invocation-model.md`).
3. Schemas are canonicalized into the IR; a schema change changes
   `runbook_digest`.
4. No expression language, no interpolation of arbitrary strings, no `eval`.
   This inherits the DAG constraint in `../architecture/batch-dag-runbooks.md`
   and V2-SEC-012 / V2-FR-016.

## Parameter declaration

Each parameter declares:

| Field | Rule |
|---|---|
| `name` | `^[a-z][a-z0-9_]{0,62}$`, unique |
| `type` | one of `string`, `integer`, `boolean`, `enum`, `resource_ref`, `object`, `array` (bounded) |
| `required` | boolean; a required parameter has no default |
| `default` | only for optional parameters; must validate against `type` |
| `constraints` | type-appropriate: `max_length`, `pattern`, `min`/`max`, `enum_values`, `max_items`, `max_depth` |
| `sensitivity` | `public` \| `internal` \| `sensitive`; `sensitive` values are redacted in evidence and never appear in labels |
| `resource_kind` | required when `type = resource_ref`; e.g. `github.repository`, `github.branch` |

Hard constraints applied by admission regardless of author intent:

- every `string` has a `max_length` (default cap 4096) and, where it names an
  external resource, a `pattern`;
- `object` has `max_depth` (cap 4) and a closed property set;
- `array` has `max_items` (cap 256);
- the total canonical parameter payload has a byte cap (`max_param_bytes`,
  recommended 64 KiB);
- `enum` may not be open-ended;
- no parameter may carry credential material. A parameter whose name or schema
  suggests secret material (`token`, `secret`, `password`, `key`, `cookie`,
  `authorization`) is rejected with `RB_SECRET_PARAMETER`. Credentials are
  resolved by capability ID through the broker (ADR-0006), never passed in.

## `resource_ref` and scope

A `resource_ref` parameter is the **only** way a caller may influence which
external resource a runbook touches. Every `resource_ref`:

- validates against `resource_kind`'s canonical form before use;
- is checked against the runbook's declared resource scope expression *and*
  the caller's authorized scope; the effective scope is the **intersection**;
- an out-of-scope value produces `RB_SCOPE_DENIED` with zero credential
  resolution and zero HTTP requests.

## Bindings

Node inputs are bound from exactly three sources:

1. a declared runbook parameter (`param:<name>`);
2. a typed output field of an upstream node (`node:<key>.<field>`);
3. a literal constant recorded in the IR.

Any other binding form — string templating, concatenation, arithmetic on
untyped values, environment lookup, filesystem lookup — is rejected with
`RB_UNSAFE_BINDING`. Type compatibility between producer and consumer is
checked statically at admission, not at runtime.

Deterministic transformation nodes, if used, are limited to the safe operation
set that OD-024 must define; until OD-024 is resolved, a runbook containing a
transformation node is rejected with `RB_TRANSFORM_UNDEFINED`.

## Output schema

The output schema is closed and field-selected. A runbook returns only declared
fields; anything a node produced beyond them is dropped by the result shaper
(ADR-0015) before the result leaves the boundary. Raw-vs-returned byte counts
are measured and reported (V2-NFR-014).

Redaction fails closed (V2-SEC-008): if the shaper cannot prove a field is
non-sensitive, the field is withheld and the omission is recorded, rather than
emitted with best-effort masking.

## Canonicalization

Schemas are canonicalized before hashing: properties sorted, defaults made
explicit, numeric forms normalized, Unicode normalized (NFC), no insignificant
whitespace. Two schemas that accept exactly the same value set but differ in
authoring style must canonicalize to identical bytes — otherwise the digest is
unstable and approvals become unusable. The exact serialization is OD-018.

## Validation errors

Validation is total and deterministic: the same input always yields the same
ordered list of reason codes. Errors never echo `sensitive` parameter values.
