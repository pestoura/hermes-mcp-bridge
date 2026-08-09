# ADR-0024 — Durable DAG State Store (closes OD-003)

> **V2 · PHASE 5 · IMPLEMENTED BEHIND `DAG_FEATURE_ENABLED` · NO V1 IMPACT**

**Status:** Accepted (Phase 5)

## Context
OD-003 asked which durable store backs DAG checkpoints, leases and approval
consumption. Phase 5 requires that (a) an execution survives process death, (b)
exactly one engine may write at a time, and (c) an approval nonce is consumed
exactly once even under concurrent admission. Phase 3 already proved
write-ahead durability with a filesystem sink, but a DAG needs compare-and-set,
which a per-record file sink cannot provide atomically.

## Decision
Use **SQLite in WAL mode, a single local file, stdlib `sqlite3` only**
(`v2/dag_store.py`). Every mutating operation runs inside `BEGIN IMMEDIATE`.
Fencing is a monotonically increasing `fence_token` column: `acquire_lease`
increments it, and `save` refuses any write carrying a token lower than the
stored one (`LEASE_FENCE_STALE`). Approval single-use is a primary key on
`(approval_id, nonce)`, so exactly one concurrent inserter wins. Every stored
record carries a `record_digest` over its canonical body; `load` recomputes it
and refuses a mismatch (`CHECKPOINT_TAMPERED`). A state document whose
`schema_version` is unknown is refused (`CHECKPOINT_SCHEMA_UNSUPPORTED`) rather
than best-effort parsed.

## Consequences
No new dependency, no daemon, no listening socket and no credential of its own —
the smallest attack surface that still provides durability and CAS. The store is
local: a multi-host engine deployment is out of scope for Phase 5 and would need
a new ADR. Checkpoints are backup- and evidence-friendly (a plain file).

## Alternatives
* **Filesystem sink reused from Phase 3** — rejected: no atomic CAS, so lease
  fencing and approval single-use would be racy.
* **PostgreSQL / Redis** — rejected for Phase 5: adds a network boundary, an
  authentication surface and an operational dependency for no capability the
  phase needs.
* **In-memory only** — rejected: contradicts the checkpoint/resume requirement.

## Security implications
The store must never hold credential material. `assert_no_secret_material`
rejects any secret-like key before a write, and A5-14 asserts the persisted body
contains no `authorization` / `bearer` / `client_secret` / `private_key` /
`password` substrings. Record digests make silent tampering detectable;
tampering is fail-closed, never a warning.

## Operational implications
The state file is a real operational asset: it must be backed up with the same
discipline as the audit ledger, and its directory permissions must not be world
readable. Corruption surfaces as a refused load, not as a silent partial resume.

## Open questions
Retention and pruning of terminal executions; multi-host coordination (would
supersede the single-file assumption).
