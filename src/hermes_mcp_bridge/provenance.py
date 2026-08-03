"""Provenance and result manifests: sanitized metadata only, never prompt/output."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any

from .protocol import (
    ProvenanceClaim,
    ResultManifest,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _canonical_json_hash(payload: Any) -> str:
    if isinstance(payload, dict):
        if "__canonical__" in payload:
            return str(payload["__canonical__"])
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


def _sign_payload(canonical_payload: str) -> tuple[str, str]:
    secret = os.environ.get("HERMES_BRIDGE_HMAC_SECRET")
    if not secret:
        return "unsigned", ""
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return "hmac-sha256", digest


def _verify_signature(canonical_payload: str, signature: str) -> bool:
    status, expected = _sign_payload(canonical_payload)
    if status == "unsigned" or not expected:
        return False
    if not signature:
        return False
    return hmac.compare_digest(expected, signature)


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
    signature_status, signature = _sign_payload(
        json.dumps(canonical, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    )
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
    if manifest.canonical_digest and not _verify_signature(
        json.dumps(canonical, separators=(",", ":"), ensure_ascii=False, sort_keys=True),
        manifest.signature or "",
    ):
        return False
    return manifest.canonical_digest == canonical_digest
