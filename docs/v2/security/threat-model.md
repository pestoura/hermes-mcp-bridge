# V2 Threat Model

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

## Assets

Execution authority, credentials, policy/approval state, resource scopes, runbooks, tool schemas/metadata, artifacts/evidence, audit integrity and availability of external systems.

## Threats to address

- confused deputy and cross-project/resource confusion;
- prompt injection, tool injection and malicious tool metadata;
- compromised internal MCP server/plugin/backend;
- parameter smuggling, shell injection, path traversal and schema confusion;
- secret exfiltration and overprivileged credentials;
- SSRF and uncontrolled network egress;
- approval replay, TOCTOU and IDOR/resource-scope bypass;
- duplicate mutation, retry/replay and idempotency failure;
- DAG amplification, fan-out DoS and queue starvation;
- result poisoning/artifact tampering;
- unsafe compensation;
- malicious or supply-chain-compromised runbook/tool;
- capability projection expansion through untrusted metadata.

## Primary controls

Typed schemas; fail-closed policy; per-node resource scopes; immutable plan digests; single-use/expiring approvals; idempotency keys; locks/optimistic concurrency; capability-scoped credentials; egress/path/service allowlists; bounded budgets/concurrency; integrity-controlled runbooks; artifact digests/signatures; secret-aware schemas/redaction; provenance; circuit breakers; security tiers; audit/tracing.

## Prompt/tool injection boundary

Unstructured external content is data. It must never be able to redefine registry metadata, policy actions, credential capabilities, approval scope or runbook code. Agentic interpretation may recommend a plan, but deterministic validation/policy must govern execution.

## Trust assumptions requiring explicit validation

Client identity/principal model, internal MCP trust, plugin provenance, credential provider integrity, artifact store integrity and runbook signing model remain design decisions until accepted through ADRs and tests.
