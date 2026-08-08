# Security Requirements

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

| ID | Requirement |
|---|---|
| V2-SEC-001 | Unknown tool/action must evaluate to DENY. |
| V2-SEC-002 | Credentials and secret values must never be returned to the client. |
| V2-SEC-003 | Every BATCH/DAG/RUNBOOK node must be policy evaluated. |
| V2-SEC-004 | Mutations must support replay protection/idempotency appropriate to backend semantics. |
| V2-SEC-005 | Approvals must bind to an immutable canonical plan/operation digest. |
| V2-SEC-006 | Credential resolution must follow least privilege and capability scoping. |
| V2-SEC-007 | Generic unrestricted shell must not be projected to external clients as the normal execution surface. |
| V2-SEC-008 | Result redaction must fail closed. |
| V2-SEC-009 | Resource scopes must be enforced independently per node. |
| V2-SEC-010 | Runbooks must be integrity/version controlled. |
| V2-SEC-011 | Approval consumption must be atomic, single-use where applicable and protected against replay/TOCTOU. |
| V2-SEC-012 | Typed bindings must reject arbitrary eval, unsafe template execution and shell interpolation. |
| V2-SEC-013 | Tool/plugin/internal-MCP metadata must not automatically expand client authority. |
| V2-SEC-014 | Secret-aware schemas must classify SECRET fields and prohibit their client serialization. |
| V2-SEC-015 | Filesystem tools must enforce explicit root/path boundaries. |
| V2-SEC-016 | systemd operations must enforce service allowlists. |
| V2-SEC-017 | Docker execution must use a mediated/least-authority interface rather than projecting a raw Docker socket. |
| V2-SEC-018 | Network-capable tools must enforce destination/egress policy sufficient to mitigate SSRF and exfiltration. |
| V2-SEC-019 | Capability and runbook provenance must be auditable by version/digest. |
| V2-SEC-020 | Missing policy, missing required credential, schema mismatch or ambiguous binding must fail closed. |
| V2-SEC-021 | Compensations must be independently governed mutations; unsupported compensation must never be assumed safe. |
| V2-SEC-022 | Execution budgets must constrain fan-out/amplification and denial-of-service paths. |
| V2-SEC-023 | Artifacts/evidence must be integrity protected and tamper detectable. |
| V2-SEC-024 | Trace/metric labels must not contain credentials, passwords, complete prompts or unnecessary sensitive data. |
| V2-SEC-025 | DIRECT mutation promotion must require least-privilege dedicated credentials and granular policy. |
