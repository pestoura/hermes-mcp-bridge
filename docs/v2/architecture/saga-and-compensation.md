# Saga and Compensation

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

V2 reuses the existing saga/evidence foundation but does not claim ACID transactions across GitHub, email, Planner, Cloudflare, Home Assistant or other heterogeneous systems.

A mutating node declares whether it is compensatable, the compensation operation, compensation safety constraints and required evidence.

Example failure after A/B succeed and C fails can lead to one of: `ROLL_BACK`, `ROLL_FORWARD`, `PARTIAL_SUCCESS`, `MANUAL_INTERVENTION`.

Compensation itself is a governed mutation: it requires schema validation, policy, scopes, credentials, locks/idempotency where applicable and evidence. The engine must never infer a safe rollback when the tool/runbook does not explicitly define one.

Unsafe or failed compensation leads to `MANUAL_INTERVENTION_REQUIRED` with sufficient evidence for an operator to understand the committed state.
