# ADR-0038 - A reasoning step proposes a typed plan; it never executes

- Status: Accepted (Phase 8)
- Related: ADR-0032 (provider plugin boundary), ADR-0036.

## Context

Invariant I7 ("no provider access from reasoning") is unenforceable if the
reasoning step is handed the gateway and merely *asked* not to use it.

## Decision

The reasoning step has the signature `AgenticContext -> AgenticProposal`.
`AgenticContext` carries a request id, an intent summary, capability ids, target
refs and the remaining budget — and nothing else; `AgenticProposal` carries a
tuple of `IntentOperation` plus token usage. Neither type has a field for a
gateway, a broker, a registry, an adapter, a credential or a header, so there is
no object on which a provider call could be made. The gate asserts both shapes
and fails if either widens.

A proposal re-enters the resolver at S0 with a decremented escalation budget, so
every safety control is applied again in full to the proposed plan.

Context shaping is fail-closed: if the shaped context would carry secret-shaped
material, shaping raises and the coordinator refuses with
`E-AGENTIC-CONTEXT-SHAPING-FAILED` rather than sending a best-effort redaction.

## Consequences

- Deterministic results already obtained are kept; only the residual sub-intent
  escalates, and the recorded coverage is the ratio actually executed
  deterministically.
- The agentic layer is testable without any provider: P8-15 asserts the isolation
  from the type shapes alone.
