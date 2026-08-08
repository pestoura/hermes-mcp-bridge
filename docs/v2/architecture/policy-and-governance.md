# Policy and Per-Node Governance

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

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
