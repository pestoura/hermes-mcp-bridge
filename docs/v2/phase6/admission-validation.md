# Admission Validation

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> See ADR-0028 (compile-once canonical IR and admission).

Admission is the single point at which a runbook may enter the registry. It is
**fail-closed, total and deterministic**: the same manifest always produces the
same verdict and the same ordered reason codes. There is no partial admission,
no warning-only mode, and no override flag.

## Ordered admission pipeline

Each stage must pass before the next runs. A failure stops the pipeline and the
later stages must be observably not executed.

| # | Stage | Representative reason codes |
|---|---|---|
| 1 | **Manifest well-formedness** — parses, declared `ir_schema_version` supported, size within cap | `RB_MALFORMED`, `RB_IR_VERSION_UNSUPPORTED`, `RB_TOO_LARGE` |
| 2 | **Identity** — `runbook_id` grammar, namespace disjoint from tools, `(id, version)` not already admitted with a different digest | `RB_ID_INVALID`, `RB_NAMESPACE_COLLISION`, `RB_DIGEST_CONFLICT` |
| 3 | **Version rules** — semantic bump correct relative to the previous version, weakening changes are MAJOR | `RB_VERSION_BUMP_INVALID` |
| 4 | **Schema validation** — closed parameter/output schemas, caps, no secret parameters, `resource_kind` present | `RB_SCHEMA_INVALID`, `RB_SECRET_PARAMETER` |
| 5 | **Graph validation** — acyclic, reachable, deterministic topological order, no orphan nodes, composition depth and cycles | `RB_GRAPH_CYCLE`, `RB_UNREACHABLE_NODE`, `RB_COMPOSITION_CYCLE` |
| 6 | **Binding validation** — only `param:` / `node:` / literal, static type compatibility, no templating or expressions | `RB_UNSAFE_BINDING`, `RB_TYPE_MISMATCH`, `RB_TRANSFORM_UNDEFINED` |
| 7 | **Reference pinning** — every tool and composed runbook pinned to an exact version; referenced entries exist and are not yanked | `RB_UNPINNED_REFERENCE`, `RB_REFERENCE_UNKNOWN`, `RB_REFERENCE_YANKED` |
| 8 | **Capability computation** — computed capability set equals declared set exactly; no admin capability | `RB_CAPABILITY_SUPERSET`, `RB_CAPABILITY_MISSING`, `RB_ADMIN_CAPABILITY_FORBIDDEN` |
| 9 | **Policy/approval class** — declared class ≥ computed aggregate; approval class strong enough | `RB_POLICY_MISSING`, `RB_POLICY_CLASS_TOO_WEAK`, `RB_APPROVAL_CLASS_TOO_WEAK` |
| 10 | **Destructive-action computation** — computed marker compared to declaration; irreversibility acceptance present | `RB_DESTRUCTIVE_UNDERDECLARED`, `RB_IRREVERSIBLE_UNACCEPTED` |
| 11 | **Rollback declaration** — every mutating node declares `rollback_support`; `AUTOMATIC` requires a registered, tested compensation | `RB_ROLLBACK_UNDECLARED`, `RB_COMPENSATION_UNREGISTERED` |
| 12 | **Timeouts and budgets** — present, bounded, internally consistent; retry class explicit; agentic budgets zero unless permitted | `RB_TIMEOUT_MISSING`, `RB_TIMEOUT_INCONSISTENT`, `RB_RETRY_CLASS_MISSING`, `RB_AGENTIC_NOT_PERMITTED` |
| 13 | **Ownership** — owner resolvable, kind adequate for the class, review cadence present and bounded | `RB_OWNER_UNRESOLVABLE`, `RB_OWNER_KIND_INSUFFICIENT`, `RB_REVIEW_CADENCE_INVALID` |
| 14 | **Test attestation** — the runbook's required tests exist, were executed against this exact digest, and passed | `RB_TESTS_MISSING`, `RB_TESTS_STALE`, `RB_TESTS_FAILED` |
| 15 | **Compile to canonical IR** — deterministic; recompiling the same manifest yields byte-identical IR | `RB_COMPILE_NONDETERMINISTIC` |
| 16 | **Digest and (optional) signature** — compute `runbook_digest`; verify signature if the registry requires one (OD-019) | `RB_DIGEST_CONFLICT`, `RB_SIGNATURE_INVALID`, `RB_SIGNATURE_REQUIRED` |
| 17 | **Registry commit** — append-only event with actor, timestamp, digest, state `ADMITTED`; snapshot hash recomputed | `RB_COMMIT_CONFLICT` |

## Properties admission must guarantee

- **No network access.** Admission is a pure function of the manifest and the
  registry state. It performs no provider call and resolves no credential.
- **Determinism.** Compile twice → identical IR bytes and identical digest. A
  non-deterministic compile is itself a rejection.
- **Exactness.** Capability sets, destructive marking and policy classes are
  computed, then compared with declarations. Declarations are checked, never
  trusted.
- **Append-only.** Admission never overwrites an existing `(id, version)`.
- **Unevaluated is failed.** A stage that could not run counts as a failure,
  not as "not applicable".

## Promotion

`ADMITTED → ACTIVE` is a separate, explicitly authorized step, requiring the
owner plus the approval class appropriate to the runbook's policy class. A
staged promotion (non-production context first) is required for
`destructive_action = true`.

Admission does not imply promotion, and promotion does not imply any gate.
