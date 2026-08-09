# ADR-0028 — Compile-Once Canonical Runbook IR and Fail-Closed Admission

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

**Status:** Proposed

## Context
ADR-0010 defines a runbook as typed, executable, versioned, validated, testable,
auditable and governed, but leaves the registry mechanics and the DSL open. A
runbook that is re-interpreted at each invocation cannot be pinned, digested or
approved reliably, and validation performed at execution time arrives after the
caller has already been told the plan is acceptable.

## Decision
A runbook is compiled **once**, at admission, into a deterministic canonical
intermediate representation (IR). `runbook_digest = SHA-256(canonical_bytes(IR))`.
Admission is a fail-closed, total, deterministic pipeline that performs zero
network access and zero credential resolution, computes capability sets, the
destructive marker and the policy aggregate, compares them with the author's
declarations, requires test attestation against the exact digest, and commits an
append-only registry event. Editorial text is excluded from the IR; every
semantic field is included. A non-deterministic compile is itself a rejection.

## Consequences
Execution reads compiled IR only, so invocation is cheap and predictable.
Authoring becomes stricter: declarations are checked, not trusted, and a
manifest that "looks right" can still be rejected. A registry, a compiler and a
snapshot hash must be built and operated.

## Alternatives
Interpret the manifest at execution time; validate lazily per node; trust author
declarations; allow warning-only admission with an override flag.

## Security implications
Prevents TOCTOU between review and execution, prevents authority expansion by
declaration (V2-SEC-013), and makes integrity/version control of runbooks
enforceable (V2-SEC-010). Zero-network admission removes a large attack surface
from the validation path.

## Operational implications
Requires a compiler with byte-stable output, a registry with append-only events,
a `runbook_snapshot_hash`, and enumerable rejection reason codes for operators.

## Open questions
The DSL (OD-002) and the canonical serialization (OD-018) remain open; the
serializer must be shared with the DAG plan digest.
