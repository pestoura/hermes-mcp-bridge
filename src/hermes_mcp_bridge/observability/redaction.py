"""Central fail-closed redaction for structured logging and diagnostics.

Rules:

* Never emit secrets: Authorization/Bearer headers, API keys, tokens, cookies,
  private keys, approval IDs in full, prompts, outputs or filesystem paths.
* Never serialize arbitrary objects with ``repr``/``str``: unknown types are
  reduced to their type name only (fail closed).
* Recursive sanitization with hard depth / breadth / length limits so a hostile
  or accidental payload cannot blow up a log line.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
TRUNCATED_SUFFIX = "…[truncated]"

MAX_DEPTH = 6
MAX_ITEMS = 50
MAX_KEYS = 50
MAX_STRING_CHARS = 512
MAX_TOTAL_CHARS = 8192

#: Field names that are always redacted, regardless of value.
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api_key",
        "apikey",
        "hermes_api_key",
        "x-api-key",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "bearer",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "cookie",
        "set-cookie",
        "session_cookie",
        "private_key",
        "signature",
        "hmac",
        "hmac_secret",
        "new_value",
        "prompt",
        "prompt_text",
        "input",
        "inputs",
        "output",
        "outputs",
        "result_text",
        "response_text",
        "content",
        "message",
        "messages",
        "history",
        "traceback",
        "stack",
        "stacktrace",
        "env",
        "environ",
        "lease_token",
        "leasetoken",
        "plan_token",
        "nonce",
    }
)

#: Field names whose values are hash/prefix-fingerprinted instead of dropped.
FINGERPRINT_FIELDS: frozenset[str] = frozenset({"approval_id", "approval"})

#: Field names carrying filesystem paths -> reduced to a non-identifying shape.
PATH_FIELDS: frozenset[str] = frozenset(
    {
        "path",
        "paths",
        "file",
        "filename",
        "filepath",
        "db_path",
        "state_db_path",
        "bridge_state_db_path",
        "lock_path",
        "changed_paths",
        "directory",
        "dir",
        "tmpdir",
        "cwd",
        "home",
    }
)

_ENV_EXTRA_FIELDS = "BRIDGE_LOG_REDACT_FIELDS"

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{4,}")
_BASIC_RE = re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}")
_AUTH_HEADER_RE = re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+")
_APIKEY_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"secret|password|passwd|token)\b\s*[:=]\s*[\"']?[^\s\"',;}]+"
)
_KEYLIKE_RE = re.compile(r"\b(?:sk|pk|ghp|gho|ghs|github_pat|xoxb|xoxp)[-_][A-Za-z0-9_\-]{8,}")
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}")
_PATH_RE = re.compile(r"(?:^|(?<=[\s\"'=(]))(?:/[A-Za-z0-9._\-]+){2,}/?")

_SAFE_SCALARS = (bool, int, float)


def _extra_fields() -> frozenset[str]:
    raw = os.environ.get(_ENV_EXTRA_FIELDS, "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def is_sensitive_key(key: str) -> bool:
    lowered = str(key).strip().lower()
    if lowered in SENSITIVE_FIELDS or lowered in _extra_fields():
        return True
    return any(
        marker in lowered
        for marker in ("secret", "password", "token", "apikey", "api_key", "authorization")
    )


def fingerprint(value: str, *, keep: int = 4) -> str:
    """Return a non-reversible short fingerprint of an identifier."""

    import hashlib

    text = str(value)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    prefix = text[:keep] if keep > 0 else ""
    return f"{prefix}…{digest}" if prefix else digest


def redact_text(value: str) -> str:
    """Scrub secret-looking substrings from free text (fail closed)."""

    text = value
    text = _PEM_RE.sub(REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _AUTH_HEADER_RE.sub(f"authorization={REDACTED}", text)
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    text = _BASIC_RE.sub(f"Basic {REDACTED}", text)
    text = _APIKEY_ASSIGN_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    text = _KEYLIKE_RE.sub(REDACTED, text)
    text = _PATH_RE.sub("[PATH]", text)
    if len(text) > MAX_STRING_CHARS:
        text = text[:MAX_STRING_CHARS] + TRUNCATED_SUFFIX
    return text


def redact_path(value: Any) -> str:
    """Reduce a filesystem path to a non-identifying shape."""

    text = str(value)
    if not text:
        return ""
    base = os.path.basename(text.rstrip("/")) or "/"
    _, ext = os.path.splitext(base)
    return f"[PATH:{ext.lstrip('.') or 'noext'}]"


def _redact_exception(exc: BaseException) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "message": redact_text(str(exc)),
    }


def sanitize(value: Any, *, _depth: int = 0, _key: str | None = None) -> Any:
    """Recursively sanitize a value into JSON-safe, secret-free data."""

    if _depth > MAX_DEPTH:
        return "[MAX_DEPTH]"

    if _key is not None:
        lowered = str(_key).strip().lower()
        if is_sensitive_key(lowered):
            return REDACTED
        if lowered in FINGERPRINT_FIELDS and isinstance(value, str):
            return fingerprint(value)
        if lowered in PATH_FIELDS:
            if isinstance(value, str):
                return redact_path(value)
            if isinstance(value, (list, tuple, set, frozenset)):
                return [redact_path(item) for item in list(value)[:MAX_ITEMS]]

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, _SAFE_SCALARS):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return None
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return f"[BYTES:{len(value)}]"
    if isinstance(value, BaseException):
        return _redact_exception(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (k, v) in enumerate(value.items()):
            if index >= MAX_KEYS:
                out["__truncated_keys__"] = True
                break
            key = str(k)
            out[key] = sanitize(v, _depth=_depth + 1, _key=key)
        return out
    if isinstance(value, (Sequence, set, frozenset)) and not isinstance(value, (str, bytes)):
        items = list(value)[:MAX_ITEMS]
        result = [sanitize(item, _depth=_depth + 1, _key=_key) for item in items]
        if len(list(value)) > MAX_ITEMS:
            result.append("[TRUNCATED_ITEMS]")
        return result
    if isinstance(value, Iterable):
        # Unknown iterable: do not consume it, expose only its type.
        return f"[{type(value).__name__}]"
    # Fail closed: never repr()/str() arbitrary objects.
    return f"[{type(value).__name__}]"


def enforce_total_size(
    payload: dict[str, Any], *, max_chars: int = MAX_TOTAL_CHARS
) -> dict[str, Any]:
    """Drop non-essential fields until the JSON payload fits the size budget."""

    import json

    essential = {"ts", "level", "event", "outcome", "duration_ms"}
    current = dict(payload)
    try:
        encoded = json.dumps(current, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return {k: v for k, v in current.items() if k in essential}
    if len(encoded) <= max_chars:
        return current
    for key in sorted(current, key=lambda k: (k in essential, k)):
        if key in essential:
            continue
        current.pop(key, None)
        current["truncated"] = True
        try:
            encoded = json.dumps(current, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            break
        if len(encoded) <= max_chars:
            break
    return current
