"""Reusable MCP payload parser for approval IDs.

This module isolates result-shape extraction from MCP tool outputs and
normalizes the candidate value into a strict approval_id string or rejects
ambiguous shapes before they reach the registry validator.
"""

from __future__ import annotations

import json
import re
from typing import Any

_APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\\-]{0,159}$")


class ApprovalIdParseError(Exception):
    """Raised when a payload does not yield a valid approval_id."""


def _normalize_approval_id_value(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ApprovalIdParseError(
            f"approval_id must be a string, got {type(raw).__name__}"
        )
    normalized = raw.strip()
    if not normalized:
        raise ApprovalIdParseError("approval_id is empty after strip")
    if normalized != raw or "\n" in normalized or "\r" in normalized:
        raise ApprovalIdParseError(
            "approval_id contains surrounding whitespace or newlines"
        )
    if (normalized.startswith('"') and normalized.endswith('"')) or (
        normalized.startswith("'") and normalized.endswith("'")
    ):
        raise ApprovalIdParseError("approval_id is surrounded by quotes")
    if not _APPROVAL_ID_RE.fullmatch(normalized):
        raise ApprovalIdParseError(
            f"approval_id does not match contract: {normalized!r}"
        )
    return normalized


def _unwrap_one(payload: Any) -> dict:
    """Return a dict payload after at most one structured wrapper hop.

    Accepted single wrappers:
    - {"result": <dict>}
    - {"structuredContent": <dict>}
    - {"structured_content": <dict>}
    - {"content": [...]} with first text item being valid JSON dict

    Rejected:
    - anything that is not dict/object
    - nested wrappers after one hop
    - lists/tuples/sets at top level
    """
    if payload is None:
        raise ApprovalIdParseError("Payload is None")

    if isinstance(payload, (list, tuple, set)):
        raise ApprovalIdParseError(
            f"Unsupported payload type: {type(payload).__name__}"
        )

    if isinstance(payload, dict):
        if "approval_id" in payload:
            return payload
        for key in ("result", "structuredContent", "structured_content"):
            if key in payload and isinstance(payload[key], dict):
                return payload[key]
        content = payload.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            text = getattr(first, "text", None)
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ApprovalIdParseError("Text content is not valid JSON") from exc
                if isinstance(parsed, dict):
                    return parsed
        raise ApprovalIdParseError(
            "Dict payload has no structuredContent/structured_content/approval_id/content/result"
        )

    if isinstance(payload, str):
        raise ApprovalIdParseError(
            "Refusing to parse raw string payload as structured result"
        )

    # Object path: attribute access only
    for key in ("structuredContent", "structured_content"):
        value = getattr(payload, key, None)
        if isinstance(value, dict):
            return value
    content = getattr(payload, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ApprovalIdParseError("Text content is not valid JSON") from exc
            if isinstance(parsed, dict):
                return parsed

    raise ApprovalIdParseError(
        "Payload has no structuredContent/structured_content or valid text content"
    )


def extract_approval_id(payload: Any) -> str:
    """Return a normalized approval_id or raise ApprovalIdParseError.

    Accepted shapes after single-hop unwrap:
    - dict with top-level `approval_id` whose value is a plain str
    - wrapper single-hop resolving to one of the above

    Rejected:
    - lists/tuples/sets at any level
    - ambiguous/multiple wrappers
    - approval_id value that is not plain str
    - raw JSON-serialized strings
    - whitespace/quotes/newlines in approval_id
    """
    candidate = _unwrap_one(payload)

    if not isinstance(candidate, dict):
        raise ApprovalIdParseError(
            f"Unsupported payload type: {type(candidate).__name__}"
        )

    if "approval_id" not in candidate:
        raise ApprovalIdParseError("approval_id not found in payload")

    raw = candidate["approval_id"]
    return _normalize_approval_id_value(raw)


def extract_structured_string_field(payload: Any, field: str) -> str:
    """Extract a top-level string field from a strict dict payload.

    Fail closed on any non-dict or non-str value.
    """
    candidate = _unwrap_one(payload)
    if not isinstance(candidate, dict):
        raise ApprovalIdParseError(
            f"Unsupported payload type: {type(candidate).__name__}"
        )
    if field not in candidate:
        raise ApprovalIdParseError(f"field not found: {field}")
    return _normalize_approval_id_value(candidate[field])


def extract_approval_id_from_mcp_result(result: Any) -> str:
    """Public helper used by smoke tests and operational scripts.

    Accepts the MCP CallToolResult-like object or dict payload and returns a
    normalized approval_id string. Raises ApprovalIdParseError on any shape
    mismatch.
    """
    return extract_approval_id(result)
