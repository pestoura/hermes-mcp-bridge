# Audit Completeness and Integrity

>
> **V2 · PHASE 9 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires `HYBRID_ACCEPTED` and all Phase 7 integrations in the production cut
> accepted. Nothing here declares production readiness.

## Completeness

Definition: `terminal_audit_records == terminal_outcomes`, over the entire
acceptance window, including refusals, validation errors, budget exhaustion,
internal errors and chaos-induced failures. Target: **100%**, measured by
independent reconciliation between the request counter and the audit store, not
by the audit component reporting on itself.

## Required fields

As specified in `../phase7/audit-and-policy.md`, plus for Phases 8–9: mode,
primary reason code, rejected branches, escalation count, deterministic coverage,
budget outcome, and evidence digest.

## Integrity

- Records are append-only; updates are new records referencing the prior id.
- Each record carries a canonical digest; a per-window chained digest allows
  detection of deletion or reordering.
- Write-ahead ordering is preserved: intent record before side effect, outcome
  record after.
- Evidence documents are sanitized, hashed and indexed; the digest chain is
  reproducible from the retained corpus.

## Redaction proof

An automated scan of the full audit and evidence corpus for credential patterns,
token shapes, raw bodies, personal data beyond opaque references and provider
stack traces must return zero findings. A single finding blocks the gate.
