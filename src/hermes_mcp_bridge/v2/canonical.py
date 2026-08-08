"""Deterministic canonical serialization primitives.

Phase 1 decision (resolves the Phase-1 slice of OD-018 only for *capability
snapshots*, not for plan/runbook digests): canonical form is **JSON, UTF-8,
sorted keys, fixed separators, no volatile values**.

Rules enforced here:

* mappings are emitted with ``sort_keys=True``;
* separators are exactly ``(",", ":")`` — no insignificant whitespace;
* ``ensure_ascii=False`` and an explicit UTF-8 encode, so the byte stream is
  independent of the platform default encoding;
* floats are rejected (they are not canonically representable across
  implementations); use integers or strings;
* arrays are **not** re-sorted here — ordering is a semantic decision of the
  caller, which sorts collections by their stable IDs before serializing;
* no timestamps, paths, hostnames or process-local values may be included.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_SEPARATORS = (",", ":")


def _reject_non_canonical(value: Any) -> Any:
    """Fail closed on values that have no stable cross-platform encoding."""
    raise TypeError(f"non-canonical value of type {type(value).__name__!r}")


def _validate(value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, int | str):
        return
    if isinstance(value, float):
        raise TypeError("floats are not canonically serializable; use int or str")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            _validate(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate(item)
        return
    raise TypeError(f"non-canonical value of type {type(value).__name__!r}")


def canonical_json_text(payload: Any) -> str:
    """Return the canonical JSON text for ``payload``."""
    _validate(payload)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=_SEPARATORS,
        ensure_ascii=False,
        allow_nan=False,
        default=_reject_non_canonical,
    )


def canonical_json_bytes(payload: Any) -> bytes:
    """Return the canonical UTF-8 byte stream for ``payload``."""
    return canonical_json_text(payload).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return the lowercase 64-hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def canonical_hash(payload: Any) -> str:
    """Canonicalize ``payload`` and return its lowercase SHA-256 hex digest."""
    return sha256_hex(canonical_json_bytes(payload))


__all__ = ["canonical_hash", "canonical_json_bytes", "canonical_json_text", "sha256_hex"]
