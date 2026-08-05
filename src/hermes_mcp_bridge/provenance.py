"""Provenance and result manifests: sanitized metadata only, never prompt/output.

Signing is delegated to :mod:`hermes_mcp_bridge.signing`, which owns key
loading (``*_FILE`` > env), rotation grace (previous key, verify-only) and the
fail-closed posture for production/security_required modes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from .protocol import (
    ProvenanceClaim,
    ResultManifest,
)
from .signing import SigningConfigError, sign, verify

__all__ = [
    "SigningConfigError",
    "build_result_manifest",
    "verify_result_manifest",
]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _canonical_json_hash(payload: Any) -> str:
    """Hash a payload deterministically.

    There is no caller-controlled override: the ``__canonical__`` bypass that
    allowed a caller to dictate the digest was removed in 0.9.0.
    """

    if isinstance(payload, dict):
        normalized = _normalize(payload)
        encoded = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    raise TypeError("unsupported payload for canonical hash")


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(value[key]) for key in sorted(value.keys())}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _canonical_payload(canonical: dict[str, Any]) -> str:
    return json.dumps(canonical, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def build_result_manifest(
    *,
    execution_id: str,
    session_id: str | None,
    status: str,
    schema_versions: dict[str, str] | None = None,
    timestamps: dict[str, str] | None = None,
    tool_manifest_hashes: list[str] | None = None,
    claims: list[ProvenanceClaim] | None = None,
    artifact_refs: list[str] | None = None,
) -> ResultManifest:
    now = _utcnow().isoformat()
    canonical = {
        "execution_id": execution_id,
        "session_id": session_id,
        "status": status,
        "schema_versions": schema_versions or {},
        "timestamps": timestamps or {"created_at": now},
        "tool_manifest_hashes": tool_manifest_hashes or [],
        "claims": [claim.model_dump() for claim in (claims or [])],
        "artifact_refs": artifact_refs or [],
    }
    canonical_digest = _canonical_json_hash(canonical)
    signature_status, signature, _key_id = sign(_canonical_payload(canonical))
    return ResultManifest(
        execution_id=execution_id,
        session_id=session_id,
        status=status,
        schema_versions=schema_versions or {},
        timestamps=timestamps or {"created_at": now},
        tool_manifest_hashes=tool_manifest_hashes or [],
        claims=claims or [],
        artifact_refs=artifact_refs or [],
        canonical_digest=canonical_digest,
        signature_status=signature_status,
        signature=signature or None,
    )


def verify_result_manifest(manifest: ResultManifest) -> bool:
    canonical = {
        "execution_id": manifest.execution_id,
        "session_id": manifest.session_id,
        "status": manifest.status,
        "schema_versions": manifest.schema_versions,
        "timestamps": manifest.timestamps,
        "tool_manifest_hashes": manifest.tool_manifest_hashes,
        "claims": [claim.model_dump() for claim in manifest.claims],
        "artifact_refs": manifest.artifact_refs,
    }
    canonical_digest = _canonical_json_hash(canonical)
    if manifest.canonical_digest != canonical_digest:
        return False
    if manifest.signature_status == "unsigned":
        # Unsigned manifests are only integrity-checked; they never assert
        # provenance. Callers requiring provenance must check signature_status.
        return manifest.signature in (None, "")
    return verify(_canonical_payload(canonical), manifest.signature or "")
