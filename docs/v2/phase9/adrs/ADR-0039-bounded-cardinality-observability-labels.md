# ADR-0039 - Observability labels are drawn from closed, bounded enumerations

- Status: Accepted (Phase 9)
- Supersedes: the `ADR-0029` proposal in the downstream design lane.
- Related: ADR-0018 (observability and token economics), ADR-0036 (deterministic resolver).

## Context

Metric label sets grow without bound when they carry request-shaped data.
Repository names, branch names, user ids, paths and request ids are attacker- or
caller-controlled, so a hostile or merely buggy request can multiply the time
series a backend must retain and can smuggle sensitive strings into telemetry.

## Decision

Every metric label value is drawn from a closed enumeration defined in code:
provider, capability id, execution mode, outcome class and reason code. Free
text is never a label. Where an unbounded set is unavoidable in principle, the
producer truncates to an explicit maximum and appends a single `OVERFLOW`
member, so the label domain stays finite by construction rather than by
convention. Bounded cardinality is asserted by the Phase 9 gate, not documented
and trusted.

## Consequences

- A new reason code requires an enum change and therefore a review.
- Request-identifying detail lives in audit records and exemplars, never in
  label values; correlation is done through the audit chain.
- A buggy request cannot flood the label space; it can only produce `OVERFLOW`.
