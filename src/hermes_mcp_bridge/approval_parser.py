"""Reusable MCP payload parser for approval IDs.

This module isolates result-shape extraction from MCP tool outputs and
normalizes the candidate value into a strict approval_id string or rejects
ambiguous shapes before they reach the registry validator.
"""

from __future__ import annotations

import json
import re
from typing import Any


_APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,159}$")


class ApprovalIdParseError(Exception):
    """Raised when a payload does not yield a valid approval_id."""


def extract_approval_id(payload: Any) -> str:
    """Return a normalized approval_id or raise ApprovalIdParseError.

    Supported shapes:
    - dict with key `approval_id` at top level;
    - wrapper dict `{"result": {"approval_id": "..."}}`;
    - nested dict with approval_id after extracting structured keys or content text.

    Rejected:
    - raw string payloads;
    - approval_id not a `str`;
    - surrounding quotes, leading/trailing whitespace or newlines;
    - raw JSON serialization (`"...", {"a":1}`);
    - dict/list payload where approval_id cannot be resolved as a plain string.
    """
    if payload is None:
        raise ApprovalIdParseError("Payload is None")

    if isinstance(payload, str):
        raise ApprovalIdParseError(
            "Refusing to parse raw string payload as structured result"
        )

    if isinstance(payload, dict):
        if "approval_id" in payload:
            raw = payload["approval_id"]
        elif "result" in payload and isinstance(payload["result"], dict):
            raw = _find_approval_id_value(payload["result"])
        else:
            raw = _find_approval_id_value(payload)
        if raw is None:
            raise ApprovalIdParseError(
                "approval_id not found in payload after normalizing top-level keys"
            )
    else:
        raise ApprovalIdParseError(
            f"Unsupported payload type: {type(payload).__name__}"
        )

    return _normalize_approval_id_value(raw)


def extract_approval_id_from_mcp_result(result: Any) -> str:
    """Public helper used by smoke tests and operational scripts.

    Accepts the MCP CallToolResult-like object or dict payload and returns a
    normalized approval_id string. Raises ApprovalIdParseError on any shape
    mismatch.
    """
    if isinstance(result, dict):
        if "approval_id" in result:
            return _normalize_approval_id_value(result["approval_id"])
        if "result" in result and isinstance(result["result"], dict):
            return extract_approval_id(result["result"])

        structured = None
        for key in ("structuredContent", "structured_content"):
            if key in result and isinstance(result[key], dict):
                structured = result[key]
                break
        if structured is not None:
            if "approval_id" in structured:
                return _normalize_approval_id_value(structured["approval_id"])
            return extract_approval_id(structured)

        text_parts: list[str] = []
        for item in result.get("content", []):
            text = getattr(item, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
        if text_parts:
            combined = "\n".join(text_parts)
            try:
                payload = json.loads(combined)
            except json.JSONDecodeError as exc:
                raise ApprovalIdParseError(
                    "Text content is not valid JSON"
                ) from exc
            return extract_approval_id(payload)

        raise ApprovalIdParseError(
            "Dict payload has no structuredContent, structured_content, approval_id, content, or result wrapper"
        )

    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        if "approval_id" in structured:
            return _normalize_approval_id_value(structured["approval_id"])
        return extract_approval_id(structured)

    text_parts = []
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            text_parts.append(text)
    if text_parts:
        combined = "\n".join(text_parts)
        try:
            payload = json.loads(combined)
        except json.JSONDecodeError as exc:
            raise ApprovalIdParseError(
                "Text content is not valid JSON"
            ) from exc
        return extract_approval_id(payload)

    raise ApprovalIdParseError(
        "MCP result object has no structuredContent/structured_content or valid text content"
    )


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


def _find_approval_id_value(payload: Any) -> Any:
    if isinstance(payload, dict):
        if "approval_id" in payload:
            return payload["approval_id"]
        for value in payload.values():
            found = _find_approval_id_value(value)
            if found is not None:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _find_approval_id_value(item)
            if found is not None:
                return found
    return None
