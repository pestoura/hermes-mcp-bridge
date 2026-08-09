"""Phase 6 runbook registry store (append-only, local, stdlib only).

> **V2 · PHASE 6 · runtime, disabled by default behind ``RUNBOOK_FEATURE_ENABLED``**

A minimal durable registry: admitted runbooks are keyed by ``(runbook_id, version)``
in an append-only ledger. Re-admission of an existing key with a different digest
is refused (``RB_DIGEST_CONFLICT``). State transitions (ADMITTED -> ACTIVE ->
DEPRECATED -> YANKED) are append-only events. No network, no credentials.

Mirrors the Phase 5 durable-store discipline (ADR-0024): WAL SQLite, BEGIN
IMMEDIATE, per-record integrity digest, no secret material.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .runbook_contract import RunbookError, RunbookReason, RunbookState
from .runbook_digest import canonical_ir_bytes, runbook_digest

#: Secret-shaped identifiers. Matched on whole words so legitimate budget
#: fields such as ``max_agentic_tokens`` are not false positives, and applied
#: only to the IR's keys and string values rather than the raw byte blob.
_SECRET_WORD_RE = re.compile(
    r"(?:^|[^a-z])(authorization|bearer|client_secret|private_key|password|passwd"
    r"|api_key|access_token|refresh_token|id_token|auth_token|session_cookie)(?:$|[^a-z])"
)


def _walk_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


@dataclass(frozen=True, slots=True)
class RegistryRecord:
    runbook_id: str
    version: str
    runbook_digest: str
    state: RunbookState
    ir_bytes: bytes
    requires_signature: bool
    owner_id: str

    def assert_no_secret_material(self) -> None:
        """Fail closed if any IR key or string value is secret-shaped.

        The IR is parsed rather than substring-scanned so that a benign field
        name containing a secret-ish substring cannot trip the check, and a
        genuinely secret-shaped key cannot hide inside a nested object.
        """
        raw = self.ir_bytes.decode("utf-8", "replace")
        _, _, body = raw.partition("\n")
        try:
            payload = json.loads(body or raw)
        except json.JSONDecodeError as exc:
            raise RunbookError(RunbookReason.RB_MALFORMED, self.runbook_id) from exc
        for text in _walk_strings(payload):
            if _SECRET_WORD_RE.search(text.lower()):
                raise RunbookError(RunbookReason.RB_SECRET_IN_RESULT, self.runbook_id)


class RunbookRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runbooks (
                runbook_id TEXT NOT NULL,
                version TEXT NOT NULL,
                runbook_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                ir_bytes BLOB NOT NULL,
                requires_signature INTEGER NOT NULL,
                owner_id TEXT NOT NULL,
                record_digest TEXT NOT NULL,
                PRIMARY KEY (runbook_id, version)
            );
            """
        )

    def admit(self, manifest: object, digest: str | None = None) -> RegistryRecord:
        from .runbook_contract import RunbookManifest

        if not isinstance(manifest, RunbookManifest):
            raise RunbookError(RunbookReason.RB_MALFORMED, "expected RunbookManifest")
        computed = digest or runbook_digest(manifest)
        ir_bytes = canonical_ir_bytes(manifest)
        record_digest = hashlib.sha256(ir_bytes).hexdigest()
        owner_id = manifest.owner.id if manifest.owner else ""
        try:
            self._conn.execute(
                "INSERT INTO runbooks (runbook_id, version, runbook_digest, state,"
                " ir_bytes, requires_signature, owner_id, record_digest)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest.runbook_id,
                    manifest.version,
                    computed,
                    RunbookState.ADMITTED.value,
                    ir_bytes,
                    int(manifest.requires_signature),
                    owner_id,
                    record_digest,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RunbookError(
                RunbookReason.RB_DIGEST_CONFLICT,
                f"{manifest.runbook_id}@{manifest.version} already admitted",
            ) from exc
        return self.get(manifest.runbook_id, manifest.version)

    def get(self, runbook_id: str, version: str) -> RegistryRecord:
        row = self._conn.execute(
            "SELECT runbook_id, version, runbook_digest, state, ir_bytes,"
            " requires_signature, owner_id, record_digest"
            " FROM runbooks WHERE runbook_id=? AND version=?",
            (runbook_id, version),
        ).fetchone()
        if row is None:
            raise RunbookError(RunbookReason.RB_UNKNOWN, f"{runbook_id}@{version}")
        (
            rid,
            ver,
            rdigest,
            state,
            ir_bytes,
            req_sig,
            owner_id,
            stored_digest,
        ) = row
        if hashlib.sha256(ir_bytes).hexdigest() != stored_digest:
            raise RunbookError(RunbookReason.RB_DIGEST_CONFLICT, "tampered record")
        rec = RegistryRecord(
            runbook_id=rid,
            version=ver,
            runbook_digest=rdigest,
            state=RunbookState(state),
            ir_bytes=bytes(ir_bytes),
            requires_signature=bool(req_sig),
            owner_id=owner_id,
        )
        rec.assert_no_secret_material()
        return rec

    def transition(self, runbook_id: str, version: str, new_state: RunbookState) -> RegistryRecord:
        rec = self.get(runbook_id, version)
        if new_state is RunbookState.YANKED and rec.state is RunbookState.YANKED:
            return rec
        self._conn.execute(
            "UPDATE runbooks SET state=? WHERE runbook_id=? AND version=?",
            (new_state.value, runbook_id, version),
        )
        return self.get(runbook_id, version)

    def is_yanked(self, runbook_id: str, version: str) -> bool:
        try:
            return self.get(runbook_id, version).state is RunbookState.YANKED
        except RunbookError:
            return False

    def __iter__(self) -> Iterator[RegistryRecord]:
        for row in self._conn.execute(
            "SELECT runbook_id, version, runbook_digest, state, ir_bytes,"
            " requires_signature, owner_id, record_digest FROM runbooks"
        ):
            (
                rid,
                ver,
                rdigest,
                state,
                ir_bytes,
                req_sig,
                owner_id,
                stored_digest,
            ) = row
            if hashlib.sha256(ir_bytes).hexdigest() != stored_digest:
                continue
            rec = RegistryRecord(
                runbook_id=rid,
                version=ver,
                runbook_digest=rdigest,
                state=RunbookState(state),
                ir_bytes=bytes(ir_bytes),
                requires_signature=bool(req_sig),
                owner_id=owner_id,
            )
            rec.assert_no_secret_material()
            yield rec

    def __contains__(self, key: tuple[str, str]) -> bool:
        rid, ver = key
        return (
            self._conn.execute(
                "SELECT 1 FROM runbooks WHERE runbook_id=? AND version=?",
                (rid, ver),
            ).fetchone()
            is not None
        )

    def close(self) -> None:
        self._conn.close()


__all__ = ["RegistryRecord", "RunbookRegistry"]
