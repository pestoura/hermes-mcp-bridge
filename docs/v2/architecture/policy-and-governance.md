# Policy and Per-Node Governance

> **V2 · PHASE 1 SUBSET IMPLEMENTED · NOT YET ACCEPTED · NO IMPACT ON V1**

Phase 1 implements only the static per-tool policy decision
(`hermes_mcp_bridge.v2.policy`). Approvals binding, plan digests, idempotency
keys, locks and quotas are **not** implemented in Phase 1.

V2 reuses v1 policy/approval/lock/quota foundations and extends them to typed tools and every BATCH/DAG/RUNBOOK node.

## Per-node governance chain

```text
principal -> resource scope -> policy -> risk/mutation class -> budget/quota
          -> approval -> lock -> credential capability -> execution -> evidence/audit
```

Example policy intent:

- `github.read.*` -> ALLOW;
- `github.create_branch` -> ALLOW or CONDITIONAL;
- `github.create_pr` -> ALLOW / CONDITIONAL;
- `github.merge_pr` -> CONDITIONAL;
- `github.delete_repo` -> DENY;
- `system.status`, `system.logs` -> ALLOW;
- `system.restart` -> CONDITIONAL;
- destructive filesystem actions -> DENY or EXPLICIT_APPROVAL.

## Approval binding

Approvals should bind to principal, resource scope, operation, canonical arguments/digest, immutable plan digest, expiry, nonce/idempotency and trust context. An approval for A must not authorize B. Approved-plan execution must consume authorization atomically.

## Plan digest

DAGs/runbooks require canonical serialization and a `plan_digest`. Approval is issued for that digest; execution must verify the same digest. Any node/argument change produces a different digest and invalidates the approval.

## Idempotency and replay protection

Mutating requests/nodes should carry an `idempotency_key` and persist the key -> execution result association. Retries must not create double merges, duplicate issues/tasks or repeated external mutations.

## Retry classes

`RETRY_SAFE`, `RETRY_CONDITIONAL`, `NO_RETRY`. Reads are usually safe; mutations depend on backend idempotency and recorded state. Rate-limit responses must honor `Retry-After`, bounded backoff, jitter and the request deadline.

## Locks/concurrency

Typed lock scopes may include repository, pull request, service or container resources. Locks support TTL, owner, renewal, expiry and safe release. Use optimistic concurrency (`ETag`, revision, resource version) when supported by the backend.

## Policy simulation

`dry_run=true` should support per-node `ALLOW`, `DENY`, `APPROVAL_REQUIRED` plus reason, without executing external mutations.

## Phase 1 policy-as-code subset

`PolicyEngine.evaluate()` is deterministic and fail closed. Phase 1 decisions:

- rules are **explicit per `policy_action`**. Wildcards and globs (`*`, `?`,
  `[`, `]`) are rejected at rule-construction time, so a permissive rule cannot
  be written at all; duplicate rules for one action are also rejected;
- the outcome set is exactly `ALLOW`, `DENY`, `APPROVAL_REQUIRED`;
- fixed evaluation order — (1) destructive/T4 backstop, (2) capability known
  and `READY`, (3) required credential capability `READY`, (4) an explicit rule
  exists, (5) the tool's own `approval_requirement`;
- unknown tool -> `DENY` (`UNKNOWN_TOOL`); missing rule -> `DENY`
  (`MISSING_POLICY_RULE`); capability not `READY` -> `DENY`
  (`CAPABILITY_NOT_READY`); credential capability missing or not `READY` ->
  `DENY` (`CREDENTIAL_CAPABILITY_UNKNOWN` / `CREDENTIAL_CAPABILITY_NOT_READY`);
- **T4/destructive is denied by default** (`DESTRUCTIVE_DENIED_BY_DEFAULT`)
  before any rule is consulted, so an accidental `ALLOW` rule cannot enable it;
- a tool declaring `approval_requirement = REQUIRED` can never resolve to a
  plain `ALLOW`; it is upgraded to `APPROVAL_REQUIRED`
  (`APPROVAL_REQUIRED_BY_TOOL`);
- `approval_requirement = CONDITIONAL` is **not** the same as `REQUIRED`: in
  Phase 1 the *condition is the policy rule*. An explicit `APPROVAL_REQUIRED`
  rule yields `APPROVAL_REQUIRED` (`APPROVAL_REQUIRED_BY_RULE`); an explicit
  `ALLOW` rule yields `ALLOW`. Richer conditional predicates (argument-aware,
  principal-aware) are deferred with the rest of OD-017;
- every decision carries a stable `ReasonCode` token containing no secret,
  path or argument value.

This is a scoped, partial answer to OD-017: the rule *model* is fixed for Phase
1, but the durable policy-as-code format and engine choice remain open.
