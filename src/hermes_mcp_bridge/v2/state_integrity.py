"""Out-of-band Hermes state-integrity measurement for the V2 Phase 2 gate.

This module is repo-side foundation only. It is **not** wired into the
canonical connected gate and it does not change V1 or the frozen 27-tool
contract.

Contract
--------

* The supplied Hermes state database is opened strictly read-only
  (``file:...?mode=ro`` plus ``PRAGMA query_only = ON``) and is never written,
  vacuumed, migrated or checkpointed by this module.
* A snapshot is a canonical, **non-secret** description of the database:
  ``user_version``/``schema_version`` pragmas, per-tracked-table existence, a
  digest of the table SQL, ``COUNT(*)`` and ``MAX(rowid)``, plus caller-supplied
  size/mtime metadata. No row content, no column values, no filesystem path.
* Comparison between two snapshots emits only booleans, integer count deltas
  and the equality result of a per-run salted digest. The salt lives in memory
  for the duration of a single run and is never returned, logged or persisted.
* Every failure is a :class:`StateIntegrityError` carrying a stable reason code
  from :data:`REASON_CODES`. Errors never contain paths, row content or salt.

True *absolute zero delta* can only be asserted out-of-band, after the Hermes
control run that observes the database has fully ended; see
``docs/v2/phase2-out-of-band-state-integrity.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .canonical import canonical_json_bytes

#: Tables tracked when present. Absence is recorded, never inferred as zero.
TRACKED_TABLES: Final[tuple[str, ...]] = (
    "sessions",
    "messages",
    "session_model_usage",
)

#: Envelope version of the snapshot shape. A change here changes every digest.
STATE_SNAPSHOT_SCHEMA: Final[str] = "hermes-v2-phase2-state-integrity/1"

#: Minimum accepted per-run salt length in bytes.
MIN_SALT_BYTES: Final[int] = 32

#: Stable, secret-free failure codes.
REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "STATE_DB_PATH_INVALID",
        "STATE_DB_NOT_REGULAR_FILE",
        "STATE_DB_UNREADABLE",
        "STATE_DB_QUERY_FAILED",
        "STATE_METADATA_INVALID",
        "STATE_SALT_INVALID",
        "STATE_SNAPSHOT_SCHEMA_MISMATCH",
        "STATE_SNAPSHOT_SALT_MISMATCH",
        "STATE_PATHS_NOT_DISJOINT",
    }
)

_SQLITE_TIMEOUT_SECONDS: Final[float] = 5.0
_SIDECAR_SUFFIXES: Final[tuple[str, ...]] = ("-wal", "-shm", "-journal")


class StateIntegrityError(RuntimeError):
    """Fail-closed state-integrity failure identified only by a stable code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if code not in REASON_CODES:  # pragma: no cover - defensive
            code = "STATE_DB_QUERY_FAILED"
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"StateIntegrityError({self.code!r})"


def new_run_salt() -> bytes:
    """Return a fresh in-memory per-run salt. Never persist or emit this."""
    return secrets.token_bytes(MIN_SALT_BYTES)


@dataclass(frozen=True, slots=True)
class StateFileMetadata:
    """Caller-supplied, path-free size/mtime metadata for the state database."""

    size_bytes: int
    mtime_ns: int

    def __post_init__(self) -> None:
        for value in (self.size_bytes, self.mtime_ns):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise StateIntegrityError("STATE_METADATA_INVALID")


@dataclass(frozen=True, slots=True)
class TableFingerprint:
    """Existence/shape/cardinality fingerprint of a single tracked table."""

    name: str
    present: bool
    schema_digest: str | None
    row_count: int | None
    max_rowid: int | None

    def as_canonical(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "present": self.present,
            "schema_digest": self.schema_digest,
            "row_count": self.row_count,
            "max_rowid": self.max_rowid,
        }


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Canonical non-secret snapshot of a Hermes state database.

    The snapshot deliberately carries no filesystem path and no row content.
    ``digest`` is salted with the per-run salt, so it is comparable only within
    the same run.
    """

    schema: str
    user_version: int
    sqlite_schema_version: int
    tables: tuple[TableFingerprint, ...]
    metadata: StateFileMetadata
    digest: str
    salt_fingerprint: str

    def table(self, name: str) -> TableFingerprint:
        for item in self.tables:
            if item.name == name:
                return item
        raise StateIntegrityError("STATE_SNAPSHOT_SCHEMA_MISMATCH")

    def as_canonical(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "user_version": self.user_version,
            "sqlite_schema_version": self.sqlite_schema_version,
            "tables": [item.as_canonical() for item in self.tables],
            "size_bytes": self.metadata.size_bytes,
            "mtime_ns": self.metadata.mtime_ns,
        }

    def __repr__(self) -> str:
        return (
            "StateSnapshot("
            f"schema={self.schema!r}, user_version={self.user_version}, "
            f"tables={len(self.tables)}, digest={self.digest[:12]!r}…)"
        )


@dataclass(frozen=True, slots=True)
class TableDelta:
    """Per-table comparison result. Counts only, never content."""

    name: str
    presence_changed: bool
    schema_changed: bool
    row_count_delta: int | None
    max_rowid_delta: int | None

    @property
    def changed(self) -> bool:
        return bool(
            self.presence_changed
            or self.schema_changed
            or (self.row_count_delta or 0)
            or (self.max_rowid_delta or 0)
        )


@dataclass(frozen=True, slots=True)
class StateComparison:
    """Booleans and integer deltas only — safe to publish as evidence."""

    digest_equal: bool
    user_version_changed: bool
    sqlite_schema_version_changed: bool
    size_changed: bool
    size_delta: int
    mtime_changed: bool
    tables: tuple[TableDelta, ...]

    @property
    def unchanged(self) -> bool:
        """True only when nothing measurable moved between the two snapshots."""
        return (
            self.digest_equal
            and not self.user_version_changed
            and not self.sqlite_schema_version_changed
            and not self.size_changed
            and not self.mtime_changed
            and not any(item.changed for item in self.tables)
        )

    def as_canonical(self) -> dict[str, Any]:
        return {
            "digest_equal": self.digest_equal,
            "user_version_changed": self.user_version_changed,
            "sqlite_schema_version_changed": self.sqlite_schema_version_changed,
            "size_changed": self.size_changed,
            "size_delta": self.size_delta,
            "mtime_changed": self.mtime_changed,
            "unchanged": self.unchanged,
            "tables": [
                {
                    "name": item.name,
                    "presence_changed": item.presence_changed,
                    "schema_changed": item.schema_changed,
                    "row_count_delta": item.row_count_delta,
                    "max_rowid_delta": item.max_rowid_delta,
                }
                for item in self.tables
            ],
        }


def _validated_salt(salt: bytes) -> bytes:
    if not isinstance(salt, bytes | bytearray) or len(salt) < MIN_SALT_BYTES:
        raise StateIntegrityError("STATE_SALT_INVALID")
    return bytes(salt)


def _salt_fingerprint(salt: bytes) -> str:
    """Return a non-reversible per-run marker used only to pair snapshots."""
    return hmac.new(salt, b"salt-fingerprint/1", hashlib.sha256).hexdigest()


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=_SQLITE_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:
        raise StateIntegrityError("STATE_DB_UNREADABLE") from exc
    try:
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        connection.close()
        raise StateIntegrityError("STATE_DB_UNREADABLE") from exc
    return connection


def _tracked_table_fingerprints(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
) -> tuple[TableFingerprint, ...]:
    fingerprints: list[TableFingerprint] = []
    for name in tables:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        if row is None:
            fingerprints.append(
                TableFingerprint(
                    name=name,
                    present=False,
                    schema_digest=None,
                    row_count=None,
                    max_rowid=None,
                )
            )
            continue
        schema_sql = row[0] or ""
        digest = hashlib.sha256(schema_sql.encode("utf-8")).hexdigest()
        count_row = connection.execute(
            # Table name comes from the fixed TRACKED_TABLES allow-list only.
            f'SELECT COUNT(*), MAX(rowid) FROM "{name}"'
        ).fetchone()
        row_count = int(count_row[0]) if count_row is not None else 0
        max_rowid = (
            int(count_row[1]) if count_row is not None and count_row[1] is not None else 0
        )
        fingerprints.append(
            TableFingerprint(
                name=name,
                present=True,
                schema_digest=digest,
                row_count=row_count,
                max_rowid=max_rowid,
            )
        )
    return tuple(fingerprints)


def capture_state_snapshot(
    db_path: str | os.PathLike[str],
    *,
    metadata: StateFileMetadata,
    salt: bytes,
    tables: tuple[str, ...] = TRACKED_TABLES,
) -> StateSnapshot:
    """Capture a canonical non-secret snapshot of a Hermes state database.

    The database is opened read-only and is never modified. ``metadata`` is
    supplied by the caller (which is the component allowed to stat the path) so
    this function never has to expose or retain the path itself.
    """
    run_salt = _validated_salt(salt)
    if not isinstance(metadata, StateFileMetadata):
        raise StateIntegrityError("STATE_METADATA_INVALID")

    try:
        path = Path(db_path)
    except TypeError as exc:
        raise StateIntegrityError("STATE_DB_PATH_INVALID") from exc
    if not path.is_absolute():
        raise StateIntegrityError("STATE_DB_PATH_INVALID")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise StateIntegrityError("STATE_DB_UNREADABLE") from exc
    if not stat.S_ISREG(info.st_mode):
        raise StateIntegrityError("STATE_DB_NOT_REGULAR_FILE")

    connection = _read_only_connection(path)
    try:
        try:
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            sqlite_schema_version = int(
                connection.execute("PRAGMA schema_version").fetchone()[0]
            )
            fingerprints = _tracked_table_fingerprints(connection, tables)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise StateIntegrityError("STATE_DB_QUERY_FAILED") from exc
    finally:
        connection.close()

    body = {
        "schema": STATE_SNAPSHOT_SCHEMA,
        "user_version": user_version,
        "sqlite_schema_version": sqlite_schema_version,
        "tables": [item.as_canonical() for item in fingerprints],
        "size_bytes": metadata.size_bytes,
        "mtime_ns": metadata.mtime_ns,
    }
    digest = hmac.new(run_salt, canonical_json_bytes(body), hashlib.sha256).hexdigest()
    return StateSnapshot(
        schema=STATE_SNAPSHOT_SCHEMA,
        user_version=user_version,
        sqlite_schema_version=sqlite_schema_version,
        tables=fingerprints,
        metadata=metadata,
        digest=digest,
        salt_fingerprint=_salt_fingerprint(run_salt),
    )


def compare_snapshots(before: StateSnapshot, after: StateSnapshot) -> StateComparison:
    """Compare two snapshots taken in the same run with the same salt."""
    if not isinstance(before, StateSnapshot) or not isinstance(after, StateSnapshot):
        raise StateIntegrityError("STATE_SNAPSHOT_SCHEMA_MISMATCH")
    if before.schema != after.schema or before.schema != STATE_SNAPSHOT_SCHEMA:
        raise StateIntegrityError("STATE_SNAPSHOT_SCHEMA_MISMATCH")
    if not hmac.compare_digest(before.salt_fingerprint, after.salt_fingerprint):
        raise StateIntegrityError("STATE_SNAPSHOT_SALT_MISMATCH")
    if tuple(item.name for item in before.tables) != tuple(
        item.name for item in after.tables
    ):
        raise StateIntegrityError("STATE_SNAPSHOT_SCHEMA_MISMATCH")

    deltas: list[TableDelta] = []
    for old, new in zip(before.tables, after.tables, strict=True):
        row_delta: int | None = None
        rowid_delta: int | None = None
        if old.present and new.present:
            row_delta = int((new.row_count or 0) - (old.row_count or 0))
            rowid_delta = int((new.max_rowid or 0) - (old.max_rowid or 0))
        deltas.append(
            TableDelta(
                name=old.name,
                presence_changed=old.present != new.present,
                schema_changed=old.schema_digest != new.schema_digest,
                row_count_delta=row_delta,
                max_rowid_delta=rowid_delta,
            )
        )

    return StateComparison(
        digest_equal=hmac.compare_digest(before.digest, after.digest),
        user_version_changed=before.user_version != after.user_version,
        sqlite_schema_version_changed=(
            before.sqlite_schema_version != after.sqlite_schema_version
        ),
        size_changed=before.metadata.size_bytes != after.metadata.size_bytes,
        size_delta=int(after.metadata.size_bytes - before.metadata.size_bytes),
        mtime_changed=before.metadata.mtime_ns != after.metadata.mtime_ns,
        tables=tuple(deltas),
    )


def read_state_metadata(db_path: str | os.PathLike[str]) -> StateFileMetadata:
    """Stat the database and return path-free size/mtime metadata."""
    try:
        info = os.lstat(Path(db_path))
    except OSError as exc:
        raise StateIntegrityError("STATE_DB_UNREADABLE") from exc
    if not stat.S_ISREG(info.st_mode):
        raise StateIntegrityError("STATE_DB_NOT_REGULAR_FILE")
    return StateFileMetadata(size_bytes=int(info.st_size), mtime_ns=int(info.st_mtime_ns))


def assert_state_paths_disjoint(
    live_db_path: str | os.PathLike[str],
    shadow_db_path: str | os.PathLike[str],
) -> None:
    """Fail closed when the shadow run could touch the live state database.

    Raises ``STATE_PATHS_NOT_DISJOINT`` when the two databases (or any of their
    SQLite sidecar files) resolve to the same filesystem object, or when one
    database lives inside the other's directory tree.
    """
    live = Path(live_db_path)
    shadow = Path(shadow_db_path)
    if not live.is_absolute() or not shadow.is_absolute():
        raise StateIntegrityError("STATE_DB_PATH_INVALID")

    def _candidates(path: Path) -> set[str]:
        base = os.path.normpath(str(path))
        return {base} | {base + suffix for suffix in _SIDECAR_SUFFIXES}

    if _candidates(live) & _candidates(shadow):
        raise StateIntegrityError("STATE_PATHS_NOT_DISJOINT")

    live_parent = os.path.normpath(str(live.parent))
    shadow_parent = os.path.normpath(str(shadow.parent))
    if live_parent == shadow_parent:
        raise StateIntegrityError("STATE_PATHS_NOT_DISJOINT")
    if (live_parent + os.sep).startswith(shadow_parent + os.sep):
        raise StateIntegrityError("STATE_PATHS_NOT_DISJOINT")
    if (shadow_parent + os.sep).startswith(live_parent + os.sep):
        raise StateIntegrityError("STATE_PATHS_NOT_DISJOINT")


__all__ = [
    "MIN_SALT_BYTES",
    "REASON_CODES",
    "STATE_SNAPSHOT_SCHEMA",
    "TRACKED_TABLES",
    "StateComparison",
    "StateFileMetadata",
    "StateIntegrityError",
    "StateSnapshot",
    "TableDelta",
    "TableFingerprint",
    "assert_state_paths_disjoint",
    "capture_state_snapshot",
    "compare_snapshots",
    "new_run_salt",
    "read_state_metadata",
]
