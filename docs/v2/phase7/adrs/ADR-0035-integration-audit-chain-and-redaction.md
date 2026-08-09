# ADR-0035 — Integration audit is a chained, append-only, structurally redacted ledger

- Status: Accepted (Phase 7)
- Related: Phase 3 write-ahead mutation audit, ADR-0029 (bounded metric labels).

## Context

Audit completeness cannot be asserted by the audit component reporting on
itself, and a substring secret scan over serialized records eventually rejects a
legitimate governance field (the `max_agentic_tokens` trap seen in Phase 6).

## Decision

Every terminal outcome — success, partial, refusal, error and unknown — produces
**exactly one** terminal audit record; a mutating capability additionally
produces an **intent** record appended before any side effect. Each record
carries `prev_digest`, forming a per-window chain whose recomputation from the
retained corpus detects deletion or reordering.

Redaction is structural and enforced **before** the sink is touched: a record
whose canonical form contains secret-shaped material is refused with
`E-AUDIT-UNAVAILABLE`. Matching is whole-name on keys (with reference suffixes
such as `_id`, `_ref`, `_digest` stripped, so `credential_capability_id` and
`scope_set_digest` are legitimate) and prefix-based on values (`Bearer `,
`ghp_`, `-----BEGIN`, …). It is never a substring scan.

Completeness is measured by independent reconciliation
(`terminal_records == terminal_outcomes`), not self-report.

## Consequences

- An unavailable audit sink refuses the write path before any side effect, and
  degrades the read path with an explicit marker.
- Reason codes are a closed enumeration (`ProviderReason`), safe as bounded
  metric labels; free text never enters a label set.
