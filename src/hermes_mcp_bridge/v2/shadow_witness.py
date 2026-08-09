"""Private shadow-state activity handoff for the V2 Phase 2 final acceptance.

Why this exists
---------------

The inner connected launcher deletes its disposable shadow home in its EXIT
trap. The OUTER final out-of-band runner therefore can never inspect the shadow
state database itself: by the time the launcher returns, the evidence is gone.
Disabling the launcher's cleanup is not acceptable — the shadow home holds live
credential-derived material and must stay disposable.

The handoff instead lets the launcher hand the outer runner a **witness**: a
sanitized, 0600 JSON document written *before* cleanup to a dedicated file
outside the shadow home. It is enabled only when the outer runner explicitly
passes :data:`WITNESS_ENV` to the launcher; a launcher started by anyone else
never writes anything.

Privacy contract
----------------

The witness carries only:

* the envelope schema and version;
* the pinned ``source_commit``;
* integer row-count deltas and derived booleans for ``sessions`` and
  ``session_model_usage`` in the **shadow** database;
* booleans proving the shadow database was distinct from the real source state
  database and lived inside the disposable shadow home.

It never carries a path, a row value, a session id, a token, a prompt or any
free-text column. Distinctness is computed inside :func:`build_witness` from
``(st_dev, st_ino)`` and the containment test; only the resulting booleans are
serialized.

Single consumption
------------------

:func:`consume_witness` validates the envelope and the expected commit, then
unlinks the file. A second call returns ``None``: a witness can never be
replayed into a second acceptance.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

SHADOW_WITNESS_SCHEMA: Final[str] = "hermes-v2-phase2-shadow-activity-witness/1"
SHADOW_WITNESS_VERSION: Final[int] = 1

#: Environment variable carrying the absolute handoff path. Set by the OUTER
#: final runner only. Its name is deliberately free of secret-bearing tokens so
#: the out-of-band argv/env scrubbers accept it.
WITNESS_ENV: Final[str] = "HERMES_V2_FINAL_SHADOW_WITNESS_FILE"

#: Shadow tables whose growth proves the shadow runtime actually did work.
WITNESS_TABLES: Final[tuple[str, ...]] = ("sessions", "session_model_usage")

REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "SHADOW_WITNESS_PATH_INVALID",
        "SHADOW_WITNESS_DB_UNREADABLE",
        "SHADOW_WITNESS_SCHEMA_UNEXPECTED",
        "SHADOW_WITNESS_WRITE_FAILED",
        "SHADOW_WITNESS_NOT_DISTINCT",
    }
)


class ShadowWitnessError(RuntimeError):
    """Fail-closed handoff failure identified only by a stable code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if code not in REASON_CODES:  # pragma: no cover - defensive
            code = "SHADOW_WITNESS_SCHEMA_UNEXPECTED"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ShadowCounts:
    """Bounded per-table row counts. Never leaves this module as a path."""

    sessions: int
    session_model_usage: int

    def as_canonical(self) -> dict[str, int]:
        return {
            "sessions": int(self.sessions),
            "session_model_usage": int(self.session_model_usage),
        }


def read_shadow_counts(shadow_state_db: str | os.PathLike[str]) -> ShadowCounts:
    """Read bounded ``COUNT(*)`` aggregates from the shadow DB, read-only.

    A missing database is reported as all-zero counts (the shadow runtime has
    not created it yet); an existing database whose witness tables are absent
    or unreadable fails closed.
    """
    path = Path(shadow_state_db)
    if not path.is_absolute():
        raise ShadowWitnessError("SHADOW_WITNESS_PATH_INVALID")
    if not path.is_file():
        return ShadowCounts(sessions=0, session_model_usage=0)
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        raise ShadowWitnessError("SHADOW_WITNESS_DB_UNREADABLE") from exc
    try:
        present = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        values: dict[str, int] = {}
        for table in WITNESS_TABLES:
            if table not in present:
                raise ShadowWitnessError("SHADOW_WITNESS_SCHEMA_UNEXPECTED")
            row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            if row is None:
                raise ShadowWitnessError("SHADOW_WITNESS_SCHEMA_UNEXPECTED")
            values[table] = int(row[0])
    except sqlite3.Error as exc:
        raise ShadowWitnessError("SHADOW_WITNESS_DB_UNREADABLE") from exc
    finally:
        connection.close()
    return ShadowCounts(
        sessions=values["sessions"],
        session_model_usage=values["session_model_usage"],
    )


def _same_file(left: Path, right: Path) -> bool:
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except OSError:
        return False
    return (left_stat.st_dev, left_stat.st_ino) == (
        right_stat.st_dev,
        right_stat.st_ino,
    )


def build_witness(
    *,
    source_commit: str,
    before: ShadowCounts,
    after: ShadowCounts,
    shadow_state_db: str | os.PathLike[str],
    source_state_db: str | os.PathLike[str],
    shadow_home: str | os.PathLike[str],
    handoff_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Build the sanitized witness document. Paths are used, never stored."""
    shadow_db = Path(shadow_state_db)
    source_db = Path(source_state_db)
    home = Path(shadow_home)
    handoff = Path(handoff_path)
    if not (
        shadow_db.is_absolute()
        and source_db.is_absolute()
        and home.is_absolute()
        and handoff.is_absolute()
    ):
        raise ShadowWitnessError("SHADOW_WITNESS_PATH_INVALID")

    distinct = not _same_file(shadow_db, source_db)
    try:
        inside_shadow_home = home in shadow_db.parents
    except OSError:  # pragma: no cover - defensive
        inside_shadow_home = False
    try:
        handoff_outside_shadow_home = home not in handoff.parents
    except OSError:  # pragma: no cover - defensive
        handoff_outside_shadow_home = False

    sessions_delta = int(after.sessions) - int(before.sessions)
    usage_delta = int(after.session_model_usage) - int(before.session_model_usage)
    return {
        "schema": SHADOW_WITNESS_SCHEMA,
        "version": SHADOW_WITNESS_VERSION,
        "source_commit": source_commit,
        "sessions_row_delta": sessions_delta,
        "session_model_usage_row_delta": usage_delta,
        "sessions_growth_positive": sessions_delta > 0,
        "session_model_usage_growth_positive": usage_delta > 0,
        "shadow_activity_observed": sessions_delta > 0 and usage_delta > 0,
        "shadow_db_distinct_from_source": bool(distinct),
        "shadow_db_inside_disposable_home": bool(inside_shadow_home),
        "handoff_outside_shadow_home": bool(handoff_outside_shadow_home),
        "paths_stored": False,
        "row_contents_stored": False,
        "session_ids_stored": False,
    }


def write_witness(handoff_path: str | os.PathLike[str], document: dict[str, Any]) -> Path:
    """Atomically write the witness with mode ``0600``."""
    destination = Path(handoff_path)
    if not destination.is_absolute():
        raise ShadowWitnessError("SHADOW_WITNESS_PATH_INVALID")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(document, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    except OSError as exc:
        raise ShadowWitnessError("SHADOW_WITNESS_WRITE_FAILED") from exc
    return destination


_REQUIRED_BOOLEANS: Final[tuple[str, ...]] = (
    "sessions_growth_positive",
    "session_model_usage_growth_positive",
    "shadow_activity_observed",
    "shadow_db_distinct_from_source",
    "shadow_db_inside_disposable_home",
    "handoff_outside_shadow_home",
)

_FORBIDDEN_SUBSTRINGS: Final[tuple[str, ...]] = (
    "path",
    "session_id",
    "token",
    "prompt",
    "secret",
    "row_content",
)


def validate_witness(document: Any, *, expected_commit: str) -> dict[str, Any] | None:
    """Return the witness when it is exactly the expected shape, else ``None``."""
    if not isinstance(document, dict):
        return None
    if document.get("schema") != SHADOW_WITNESS_SCHEMA:
        return None
    if document.get("version") != SHADOW_WITNESS_VERSION:
        return None
    if document.get("source_commit") != expected_commit:
        return None
    for key in _REQUIRED_BOOLEANS:
        if document.get(key) is not True:
            return None
    for key in ("sessions_row_delta", "session_model_usage_row_delta"):
        value = document.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return None
    for key in ("paths_stored", "row_contents_stored", "session_ids_stored"):
        if document.get(key) is not False:
            return None
    for key, value in document.items():
        lowered = str(key).lower()
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            if forbidden in lowered and not lowered.endswith("_stored"):
                return None
        if isinstance(value, str) and key not in ("schema", "source_commit"):
            return None
    return dict(document)


def consume_witness(
    handoff_path: str | os.PathLike[str], *, expected_commit: str
) -> dict[str, Any] | None:
    """Validate, then delete, the witness. Returns ``None`` when unusable.

    The file is removed whether or not it validated, so a rejected or replayed
    witness can never influence a later run.
    """
    path = Path(handoff_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = None
    validated = (
        validate_witness(payload, expected_commit=expected_commit) if payload is not None else None
    )
    with contextlib.suppress(OSError):
        path.unlink()
    return validated


__all__ = [
    "REASON_CODES",
    "SHADOW_WITNESS_SCHEMA",
    "SHADOW_WITNESS_VERSION",
    "WITNESS_ENV",
    "WITNESS_TABLES",
    "ShadowCounts",
    "ShadowWitnessError",
    "build_witness",
    "consume_witness",
    "read_shadow_counts",
    "validate_witness",
    "write_witness",
]
