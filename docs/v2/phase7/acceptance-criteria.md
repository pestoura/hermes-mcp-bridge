# Phase 7 Per-Integration Acceptance Criteria (fail-closed)

>
> **V2 · PHASE 7 · implemented, disabled by default behind `PROVIDER_FEATURE_ENABLED`**
>
> Predecessor gates `DIRECT_MUTATION_ACCEPTED`, `BATCH_ACCEPTED`, `DAG_ACCEPTED`
> and `RUNBOOK_ACCEPTED` are accepted. Only `github` and `jira` are in the
> provider allow-list; every other provider stays `CANDIDATE` or
> `BLOCKED_UNCONFIRMED` and is refused at registration.

A provider gate `INTEGRATION_<X>_ACCEPTED` is satisfied only when **all** of the
following hold, each with retained sanitized evidence and SHA-256 digests.

1. `RUNBOOK_ACCEPTED` (and all earlier gates) declared; recorded in the evidence
   manifest by commit SHA.
2. Provider manifest validates: every capability maps to a registered typed tool;
   no scope exceeds its credential domain; egress host allow-list exact.
3. Dedicated least-privilege credential provisioned in its own domain, scope-set
   digest recorded, `broad_credential=false`, no secret material in evidence.
4. Health probe executed against the authorized target set; readiness states
   recorded; no write capability accepted in `DEGRADED`.
5. Fail-closed ordering proven: scope/policy/readiness denial produces zero
   provider calls; scope denial additionally produces zero credential
   resolutions.
6. Full test matrix `P7-01..P7-20` executed with zero failures.
7. Every terminal outcome, including refusals, produced exactly one audit record;
   audit completeness = 100% over the acceptance run.
8. Redaction scan over evidence, audit and metric label sets: zero findings.
9. Determinism: capability snapshot hash and `write_capability_digest` identical
   across two independent runs on the same commit.
10. V1 isolation: V1 surface remains exactly 27 tools; no V1 module imports a
    Phase 7 provider.
11. Rollback: the provider can be disabled by allow-list removal alone, verified
    by a run showing `E-PROVIDER-UNKNOWN` and zero side effects.
12. Connected evidence recorded on the real host, sanitized, with bridge/schema
    version, provider call counts, token accounting (expected: zero Hermes LLM
    tokens on DIRECT paths) and mutation/cleanup residual count 0.

Not satisfied by: code existing, tests written but not executed, or a successful
manual call without retained evidence.
