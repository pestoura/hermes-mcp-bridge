"""Internal-tool provenance for the V2 Phase 2 connected acceptance samples.

Additive assurance layer: for one accepted connected sample it recovers, from
the **disposable shadow** state database and scoped strictly to that sample's
session, exactly one authorized GitHub MCP tool call and its matching result,
and proves that the internally observed result normalizes to the same digest as
the DIRECT read.

Hard privacy contract
---------------------

Nothing that identifies or reproduces the internal conversation may leave this
module. In particular the returned record never carries:

* ``tool_call_id`` or any message row id;
* the session id;
* raw arguments, raw argument values, or the raw result;
* any filesystem path.

Only booleans, the canonical tool id, normalized digests, the normalization
profile id, a coarse result size bucket and stable blocker codes are persisted.

Fail-closed
-----------

Provenance is *additive*: it can only ever keep a sample from being accepted.
The final LLM semantic digest/match stays the hard gate, and a provenance PASS
can never rescue a semantic FAIL — the caller applies semantic matching first
and this module never inspects or reports the semantic verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .canonical import canonical_json_bytes
from .shadow_isolation import SHADOW_HERMES_TOOL_NAMES, SHADOW_MCP_TOOL_NAMES

#: Envelope version of the provenance record shape.
PROVENANCE_SCHEMA: Final[str] = "hermes-v2-phase2-tool-provenance/1"

#: Identifier of the normalization profile applied to the internal result. It
#: must be the very same projection used for the DIRECT/shadow comparison.
NORMALIZATION_PROFILE_ID: Final[str] = "hermes-v2-phase2-direct-read-normalization/1"

#: Canonical tool id  ->  authorized Hermes-side MCP tool name.
CANONICAL_TO_SHADOW_TOOL: Final[dict[str, str]] = {
    name.replace("_", ".", 1): hermes_name
    for name, hermes_name in zip(SHADOW_MCP_TOOL_NAMES, SHADOW_HERMES_TOOL_NAMES, strict=True)
}
#: Authorized Hermes-side MCP tool name  ->  canonical tool id.
SHADOW_TO_CANONICAL_TOOL: Final[dict[str, str]] = {
    hermes_name: canonical for canonical, hermes_name in CANONICAL_TO_SHADOW_TOOL.items()
}
#: Bare MCP tool names are also accepted as the authorized surface.
BARE_TO_CANONICAL_TOOL: Final[dict[str, str]] = {
    name: name.replace("_", ".", 1) for name in SHADOW_MCP_TOOL_NAMES
}

#: Coarse, non-reversible result size buckets (bytes of the raw result text).
RESULT_SIZE_BUCKETS: Final[tuple[tuple[str, int], ...]] = (
    ("EMPTY", 0),
    ("XS", 512),
    ("S", 4096),
    ("M", 32768),
    ("L", 262144),
)
RESULT_SIZE_BUCKET_XL: Final[str] = "XL"

#: Stable, secret-free blocker codes.
BLOCKER_CODES: Final[frozenset[str]] = frozenset(
    {
        "PROVENANCE_SESSION_SCOPE_INVALID",
        "PROVENANCE_STATE_DB_UNREADABLE",
        "PROVENANCE_MESSAGES_TABLE_MISSING",
        "PROVENANCE_TOOL_CALLS_UNPARSEABLE",
        "PROVENANCE_NO_AUTHORIZED_TOOL_CALL",
        "PROVENANCE_MULTIPLE_AUTHORIZED_TOOL_CALLS",
        "PROVENANCE_UNAUTHORIZED_TOOL_CALL",
        "PROVENANCE_TOOL_MISMATCH",
        "PROVENANCE_ARGUMENTS_UNPARSEABLE",
        "PROVENANCE_ARG_SHAPE_MISMATCH",
        "PROVENANCE_TARGET_MISMATCH",
        "PROVENANCE_RESULT_MISSING",
        "PROVENANCE_RESULT_UNPARSEABLE",
        "PROVENANCE_NORMALIZATION_FAILED",
        "PROVENANCE_DIGEST_MISMATCH",
    }
)

_MAX_ROWS: Final[int] = 5000

#: Hermes wraps high-risk tool results (all ``mcp__*`` tools included) in a
#: prompt-injection guard envelope before persisting them. Provenance has to
#: recover the inner payload, and only from a complete, well-formed envelope.
_UNTRUSTED_ENVELOPE_RE: Final[re.Pattern[str]] = re.compile(
    r'<untrusted_tool_result source="(?P<source>[^"<>]{1,128})">\n'
    r"(?P<body>.*)\n</untrusted_tool_result>",
    re.DOTALL,
)


class ProvenanceError(RuntimeError):
    """Fail-closed provenance failure identified only by a stable code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if code not in BLOCKER_CODES:  # pragma: no cover - defensive
            code = "PROVENANCE_STATE_DB_UNREADABLE"
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ProvenanceError({self.code!r})"


def result_size_bucket(size_bytes: int) -> str:
    """Return the coarse bucket for a raw result size. Never the exact size."""
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
        return RESULT_SIZE_BUCKET_XL
    for name, upper in RESULT_SIZE_BUCKETS:
        if size_bytes <= upper:
            return name
    return RESULT_SIZE_BUCKET_XL


def arguments_shape_digest(arguments: Mapping[str, Any]) -> str:
    """Digest the *shape* of an argument mapping: keys plus value type names."""
    shape = {str(key): type(value).__name__ for key, value in sorted(arguments.items())}
    return hashlib.sha256(canonical_json_bytes(shape)).hexdigest()


def arguments_value_digest(arguments: Mapping[str, Any]) -> str:
    """Digest the canonical argument mapping. The values never leave here."""
    canonical = {str(key): value for key, value in sorted(arguments.items())}
    return hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolProvenanceRecord:
    """Sanitized, publishable provenance record for exactly one sample."""

    canonical_tool_id: str
    authorized_tool_call_count: int
    arguments_shape_sha256: str
    arguments_value_sha256: str
    internal_normalized_sha256: str
    direct_normalized_sha256: str
    result_size_bucket: str

    @property
    def digest_equal(self) -> bool:
        return self.internal_normalized_sha256 == self.direct_normalized_sha256

    def as_canonical(self) -> dict[str, Any]:
        return {
            "schema": PROVENANCE_SCHEMA,
            "provenance_pass": True,
            "canonical_tool_id": self.canonical_tool_id,
            "authorized_tool_call_count": self.authorized_tool_call_count,
            "unauthorized_tool_calls_observed": False,
            "normalization_profile_id": NORMALIZATION_PROFILE_ID,
            "arguments_shape_sha256": self.arguments_shape_sha256,
            "arguments_value_sha256": self.arguments_value_sha256,
            "internal_normalized_sha256": self.internal_normalized_sha256,
            "direct_normalized_sha256": self.direct_normalized_sha256,
            "internal_matches_direct": self.digest_equal,
            "result_size_bucket": self.result_size_bucket,
            "tool_call_id_stored": False,
            "raw_arguments_stored": False,
            "raw_result_stored": False,
            "session_id_stored": False,
            "message_rows_stored": False,
            "blockers": [],
        }


def blocked_record(code: str) -> dict[str, Any]:
    """Return the sanitized failing provenance document for a stable code."""
    if code not in BLOCKER_CODES:  # pragma: no cover - defensive
        code = "PROVENANCE_STATE_DB_UNREADABLE"
    return {
        "schema": PROVENANCE_SCHEMA,
        "provenance_pass": False,
        "normalization_profile_id": NORMALIZATION_PROFILE_ID,
        "tool_call_id_stored": False,
        "raw_arguments_stored": False,
        "raw_result_stored": False,
        "session_id_stored": False,
        "message_rows_stored": False,
        "blockers": [code],
    }


def _read_only_connection(db_path: str) -> sqlite3.Connection:
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        raise ProvenanceError("PROVENANCE_STATE_DB_UNREADABLE") from exc
    return connection


def _decode_json(value: Any) -> Any:
    if isinstance(value, bytes | bytearray):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _iter_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Normalize the stored ``tool_calls`` blob into a list of call dicts."""
    decoded = _decode_json(raw)
    if decoded is None:
        raise ProvenanceError("PROVENANCE_TOOL_CALLS_UNPARSEABLE")
    if isinstance(decoded, dict):
        decoded = [decoded]
    if not isinstance(decoded, list):
        raise ProvenanceError("PROVENANCE_TOOL_CALLS_UNPARSEABLE")
    calls: list[dict[str, Any]] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise ProvenanceError("PROVENANCE_TOOL_CALLS_UNPARSEABLE")
        function = item.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            arguments = function.get("arguments")
        else:
            name = item.get("name")
            arguments = item.get("arguments", item.get("args"))
        calls.append(
            {
                "name": str(name) if isinstance(name, str) else None,
                "arguments": arguments,
                "id": item.get("id") or item.get("tool_call_id"),
            }
        )
    return calls


def _canonical_for(name: str | None) -> str | None:
    if not isinstance(name, str):
        return None
    return SHADOW_TO_CANONICAL_TOOL.get(name) or BARE_TO_CANONICAL_TOOL.get(name)


def _unwrap_untrusted_envelope(text: str, tool_name: str | None) -> str:
    """Strip the Hermes ``<untrusted_tool_result>`` framing, fail-closed.

    Hermes wraps results from high-risk tools (including every ``mcp__*`` tool)
    in a prompt-injection guard envelope before persisting the message row, so
    the stored tool result is no longer bare JSON. Provenance must read the
    payload the model actually received, not the framing.

    The unwrap is deliberately strict: the envelope must be the whole string,
    the ``source`` attribute must equal the authorized tool call's own name, and
    exactly one closing delimiter may be present. Anything else is returned
    unchanged, so a forged partial envelope cannot smuggle a different payload
    past the digest comparison.
    """
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped.startswith("<untrusted_tool_result "):
        return text
    if not isinstance(tool_name, str) or not tool_name:
        return text
    match = _UNTRUSTED_ENVELOPE_RE.fullmatch(stripped)
    if match is None:
        return text
    if match.group("source") != tool_name:
        return text
    body = match.group("body")
    if "</untrusted_tool_result>" in body:
        return text
    _, separator, remainder = body.partition("\n\n")
    return (remainder if separator else body).strip()


def collect_tool_provenance(
    *,
    shadow_state_db: str,
    session_id: str,
    expected_tool_id: str,
    expected_arguments: Mapping[str, Any],
    direct_normalized_sha256: str,
    normalizer: Callable[[str, Any], str],
    authorized_tools: Sequence[str] | None = None,
) -> ToolProvenanceRecord:
    """Recover and verify the single authorized internal tool call.

    ``normalizer(tool_id, data) -> sha256`` must be the very same projection the
    DIRECT/shadow comparison uses; the caller supplies it so this module never
    has to know the tool schemas.
    """
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 128:
        raise ProvenanceError("PROVENANCE_SESSION_SCOPE_INVALID")
    allowed = set(authorized_tools or SHADOW_TO_CANONICAL_TOOL) | set(BARE_TO_CANONICAL_TOOL)
    if expected_tool_id not in CANONICAL_TO_SHADOW_TOOL:
        raise ProvenanceError("PROVENANCE_TOOL_MISMATCH")

    connection = _read_only_connection(shadow_state_db)
    try:
        try:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")}
        except sqlite3.Error as exc:
            raise ProvenanceError("PROVENANCE_STATE_DB_UNREADABLE") from exc
        required = {"session_id", "role", "content", "tool_calls", "tool_call_id"}
        if not required <= columns:
            raise ProvenanceError("PROVENANCE_MESSAGES_TABLE_MISSING")
        has_tool_name = "tool_name" in columns
        name_column = "tool_name" if has_tool_name else "NULL"
        try:
            rows = connection.execute(
                "SELECT role, content, tool_calls, tool_call_id, "
                f"{name_column} FROM messages WHERE session_id = ? "
                "ORDER BY rowid ASC LIMIT ?",
                (session_id, _MAX_ROWS),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ProvenanceError("PROVENANCE_STATE_DB_UNREADABLE") from exc
    finally:
        connection.close()

    authorized: list[dict[str, Any]] = []
    for role, _content, tool_calls, _call_id, _name in rows:
        if str(role) != "assistant" or tool_calls in (None, ""):
            continue
        for call in _iter_tool_calls(tool_calls):
            name = call["name"]
            if name is None:
                raise ProvenanceError("PROVENANCE_TOOL_CALLS_UNPARSEABLE")
            canonical = _canonical_for(name)
            if canonical is None:
                raise ProvenanceError("PROVENANCE_UNAUTHORIZED_TOOL_CALL")
            if name not in allowed:
                raise ProvenanceError("PROVENANCE_UNAUTHORIZED_TOOL_CALL")
            call["canonical"] = canonical
            authorized.append(call)

    if not authorized:
        raise ProvenanceError("PROVENANCE_NO_AUTHORIZED_TOOL_CALL")
    if len(authorized) > 1:
        raise ProvenanceError("PROVENANCE_MULTIPLE_AUTHORIZED_TOOL_CALLS")

    call = authorized[0]
    if call["canonical"] != expected_tool_id:
        raise ProvenanceError("PROVENANCE_TOOL_MISMATCH")

    arguments = _decode_json(call["arguments"])
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ProvenanceError("PROVENANCE_ARGUMENTS_UNPARSEABLE")

    expected = dict(expected_arguments)
    if arguments_shape_digest(arguments) != arguments_shape_digest(expected):
        raise ProvenanceError("PROVENANCE_ARG_SHAPE_MISMATCH")
    if arguments_value_digest(arguments) != arguments_value_digest(expected):
        raise ProvenanceError("PROVENANCE_TARGET_MISMATCH")

    call_id = call.get("id")
    result_text: str | None = None
    for role, content, _tool_calls, tool_call_id, tool_name in rows:
        if str(role) != "tool":
            continue
        if call_id is not None and tool_call_id is not None:
            if str(tool_call_id) != str(call_id):
                continue
        elif has_tool_name and tool_name is not None:  # noqa: SIM102
            if _canonical_for(str(tool_name)) != expected_tool_id:
                continue
        if isinstance(content, bytes | bytearray):
            content = content.decode("utf-8", errors="replace")
        if isinstance(content, str):
            result_text = content
            break
    if result_text is None:
        raise ProvenanceError("PROVENANCE_RESULT_MISSING")

    unwrapped_text = _unwrap_untrusted_envelope(result_text, call["name"])
    payload = _decode_json(unwrapped_text)
    if not isinstance(payload, dict) or not payload:
        raise ProvenanceError("PROVENANCE_RESULT_UNPARSEABLE")
    data = payload
    for key in ("structuredContent", "structured_content", "result", "data"):
        inner = data.get(key)
        if isinstance(inner, str):
            decoded_inner = _decode_json(inner)
            if isinstance(decoded_inner, dict) and decoded_inner:
                data = decoded_inner
                break
            continue
        inner = data.get(key)
        if isinstance(inner, dict) and inner:
            data = inner
            break

    try:
        internal_digest = normalizer(expected_tool_id, data)
    except Exception as exc:
        raise ProvenanceError("PROVENANCE_NORMALIZATION_FAILED") from exc
    if not isinstance(internal_digest, str) or len(internal_digest) != 64:
        raise ProvenanceError("PROVENANCE_NORMALIZATION_FAILED")
    if internal_digest != direct_normalized_sha256:
        raise ProvenanceError("PROVENANCE_DIGEST_MISMATCH")

    return ToolProvenanceRecord(
        canonical_tool_id=expected_tool_id,
        authorized_tool_call_count=1,
        arguments_shape_sha256=arguments_shape_digest(expected),
        arguments_value_sha256=arguments_value_digest(expected),
        internal_normalized_sha256=internal_digest,
        direct_normalized_sha256=direct_normalized_sha256,
        result_size_bucket=result_size_bucket(len(result_text.encode("utf-8"))),
    )


__all__ = [
    "BARE_TO_CANONICAL_TOOL",
    "BLOCKER_CODES",
    "CANONICAL_TO_SHADOW_TOOL",
    "NORMALIZATION_PROFILE_ID",
    "PROVENANCE_SCHEMA",
    "RESULT_SIZE_BUCKETS",
    "RESULT_SIZE_BUCKET_XL",
    "SHADOW_TO_CANONICAL_TOOL",
    "ProvenanceError",
    "ToolProvenanceRecord",
    "arguments_shape_digest",
    "arguments_value_digest",
    "blocked_record",
    "collect_tool_provenance",
    "result_size_bucket",
]
