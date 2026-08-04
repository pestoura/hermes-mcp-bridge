"""Central fail-closed redaction for structured logging and diagnostics.

Rules:

* Never emit secrets: Authorization/Proxy-Authorization headers, API keys,
  tokens, cookies, private keys, approval IDs in full, prompts, outputs or
  filesystem paths.
* Never serialize arbitrary objects with ``repr``/``str``: unknown types are
  reduced to their type name only (fail closed).
* Redaction is context-aware: only values bound to sensitive key names, or
  well-known credential shapes (Bearer/Basic/Digest, JWT, PEM, key prefixes),
  are scrubbed. Arbitrary benign hashes/identifiers are left intact.
* Recursive sanitization with hard depth / breadth / length limits and cycle
  protection so a hostile or accidental payload cannot blow up a log line.

The output of ``redact_text`` is canonical and idempotent: re-running it on
already-redacted text leaves the text unchanged and never produces ``]]`` or
other doubled closers.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
TRUNCATED_SUFFIX = "…[truncated]"

MAX_DEPTH = 8
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
        "pwd",
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
        "body",
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

#: Key/assignment names whose bound value is a secret (not a benign id).
SECRET_KEY_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api_key",
        "apikey",
        "x-api-key",
        "hermes_api_key",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "bearer",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "cookie",
        "set-cookie",
        "private_key",
        "hmac",
        "hmac_secret",
        "lease_token",
        "leasetoken",
        "plan_token",
        "nonce",
    }
)

#: Cookie names that are not secrets and may keep their value in free text.
_BENIGN_COOKIE_NAMES: frozenset[str] = frozenset(
    {
        "lang",
        "locale",
        "theme",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "path",
        "samesite",
        "max-age",
        "expires",
        "domain",
    }
)

_ENV_EXTRA_FIELDS = "BRIDGE_LOG_REDACT_FIELDS"

# Auth headers, with or without a scheme, in ':' or '=' form. The full value
# (to end of line, newline or ';') is consumed and redacted in one pass so a
# later rule cannot re-consume the redacted placeholder.
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(?:proxy[-_]?authorization|authorization)\b\s*[:=]\s*\S[^\r\n;]*"
)
# Bare auth-scheme tokens (Bearer/Basic/Digest) followed by a credential.
_SCHEME_CRED_RE = re.compile(
    r"(?i)\b(bearer|basic|digest)\s+[A-Za-z0-9._\-+/=]{6,}"
)
# Cookie / Set-Cookie headers; the whole value list is captured and each pair
# value is redacted separately (preserving non-secret names).
_COOKIE_RE = re.compile(
    r"(?i)\b(?:set[-_]?cookie|cookie)\b\s*[:=]\s*\S[^\r\n]*"
)
# key=value / key: value assignments for sensitive names, with optional quotes
# around the key (JSON-style "key": "value") and the value. The value class
# excludes '[' so an already-redacted placeholder ([REDACTED]) is never
# re-consumed; this makes the rule idempotent and prevents doubled closers.
#
# Escape awareness: JSON embedded in free text may arrive at escape level 0
# (``"key"``), level 1 (``\"key\"``) or level 2 (``\\"key\\"``). Instead of a
# destructive global unescape, the quote groups accept an optional run of
# backslashes before the quote character. The closing quote is matched with a
# backreference so the original escape level and structure are preserved
# verbatim and only the value is replaced.
_Q = r"\\{0,4}[\"']|"
_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<q1>" + _Q + r")(?<![\w-])(?P<key>api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|secret|password|passwd|pwd|token|"
    r"authorization|auth|cookie|bearer|apikey|x-api-key|private[_-]?key|hmac|"
    r"hmac[_-]?secret|nonce|lease[_-]?token|plan[_-]?token)(?![\w-])"
    r"(?P<q2>" + _Q + r")(?P<ws1>\s*)(?P<sep>[:=])(?P<ws2>\s*)(?P<q3>" + _Q + r")"
    r"(?P<val>[^\s\"',;}\]\[\\]+)(?P=q3)"
)


def _redact_kv(m: re.Match[str]) -> str:
    return (
        f"{m.group('q1')}{m.group('key')}{m.group('q2')}"
        f"{m.group('ws1')}{m.group('sep')}{m.group('ws2')}"
        f"{m.group('q3')}{REDACTED}{m.group('q3')}"
    )
_KEYLIKE_RE = re.compile(r"\b(?:sk|pk|ghp|gho|ghs|github_pat|xoxb|xoxp)[-_][A-Za-z0-9_\\-]{8,}")
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\\-]{8,}\.[A-Za-z0-9_\\-]{8,}\.[A-Za-z0-9_\\-]{4,}")
_PATH_RE = re.compile(r"(?:^|(?<=[\s\"'=(]))(?:/[A-Za-z0-9._\\-]+){2,}/?")

_SAFE_SCALARS = (bool, int, float)


def _extra_fields() -> frozenset[str]:
    raw = os.environ.get(_ENV_EXTRA_FIELDS, "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def is_sensitive_key(key: str) -> bool:
    lowered = str(key).strip().lower()
    if (
        lowered in SECRET_KEY_NAMES
        or lowered in SENSITIVE_FIELDS
        or lowered in _extra_fields()
    ):
        return True
    # Narrow substring markers tied to secrets only (avoid benign ids like
    # session_id or run_id which are not secrets).
    return any(
        marker in lowered
        for marker in ("secret", "password", "apikey", "api_key", "authorization")
    )


def fingerprint(value: str, *, keep: int = 4) -> str:
    """Return a non-reversible short fingerprint of an identifier."""

    text = str(value)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    prefix = text[:keep] if keep > 0 else ""
    return f"{prefix}…{digest}" if prefix else digest


#: Cookie attribute names that are directives, not credential pairs.
_COOKIE_ATTR_NAMES: frozenset[str] = frozenset(
    {"path", "samesite", "max-age", "expires", "domain", "version", "comment"}
)
#: Valueless cookie flags that must survive redaction untouched.
_COOKIE_FLAGS: frozenset[str] = frozenset({"httponly", "secure", "partitioned"})

# A syntactically legal cookie name (RFC 6265 token, pragmatically narrowed).
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _redact_cookie_value(value: str) -> str:
    """Redact a Cookie/Set-Cookie value list, fail-closed on malformed input.

    Each ';'-separated segment must be either a valueless flag (HttpOnly,
    Secure) or an analysable ``name=value`` pair. Benign names (lang, theme,
    locale, utm_*) and attribute directives (Path, SameSite, Max-Age, Expires,
    Domain) keep their value; every other pair value becomes ``[REDACTED]``.

    If no segment is an analysable pair or a known flag -- i.e. the header is
    bare or malformed, such as ``Cookie: abc123`` -- the entire value is
    replaced with ``[REDACTED]`` rather than being emitted verbatim.
    """

    segments = value.split(";")
    rendered: list[str] = []
    analysable = False

    for segment in segments:
        stripped = segment.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered in _COOKIE_FLAGS:
            analysable = True
            rendered.append(stripped)
            continue
        if stripped == REDACTED:
            # Already redacted: idempotent passthrough, not fresh evidence
            # of a well-formed header.
            rendered.append(stripped)
            continue
        name, sep, raw_val = stripped.partition("=")
        name = name.strip()
        if not sep or not _COOKIE_NAME_RE.match(name):
            # Malformed segment: fail closed for the whole header.
            return REDACTED
        lname = name.lower()
        val = raw_val.strip()
        quote = ""
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            quote, val = '"', val[1:-1]
        if lname in _COOKIE_ATTR_NAMES:
            # Attribute directive: value may contain spaces/commas (Expires).
            analysable = True
            rendered.append(stripped)
            continue
        if '"' in val or " " in val or ";" in val:
            # Unparseable pair value: fail closed for the whole header.
            return REDACTED
        analysable = True
        if (
            val == REDACTED
            or lname in _BENIGN_COOKIE_NAMES
            or lname.startswith("utm_")
        ):
            rendered.append(stripped)
        else:
            rendered.append(f"{name}={quote}{REDACTED}{quote}")

    if not analysable:
        return REDACTED
    return "; ".join(rendered)


def redact_text(value: str) -> str:
    """Scrub secret-looking substrings from free text (fail closed)."""

    text = value
    text = _PEM_RE.sub(REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _AUTH_HEADER_RE.sub(
        lambda m: f"{m.group(0).split(':', 1)[0].split('=', 1)[0].strip()} {REDACTED}",
        text,
    )
    text = _SCHEME_CRED_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _COOKIE_RE.sub(lambda m: _redact_cookie_header(m.group(0)), text)
    text = _KEY_VALUE_RE.sub(_redact_kv, text)
    text = _KEYLIKE_RE.sub(REDACTED, text)
    text = _PATH_RE.sub("[PATH]", text)
    if len(text) > MAX_STRING_CHARS:
        text = text[:MAX_STRING_CHARS] + TRUNCATED_SUFFIX
    return text


def _redact_cookie_header(header: str) -> str:
    """Redact the value portion of a Cookie/Set-Cookie header string."""

    sep = ":" if ":" in header else "="
    name, _, rest = header.partition(sep)
    return f"{name.strip()} {_redact_cookie_value(rest.strip())}"


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


def sanitize(
    value: Any,
    *,
    _depth: int = 0,
    _key: str | None = None,
    _seen: set[int] | None = None,
) -> Any:
    """Recursively sanitize a value into JSON-safe, secret-free data."""

    if _seen is None:
        _seen = set()
    if isinstance(value, (Mapping, list, dict, tuple, set, frozenset)) and id(value) in _seen:
        return "[CYCLE]"
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
                return [item for item in (redact_path(i) for i in list(value)[:MAX_ITEMS])]

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, _SAFE_SCALARS):
        if isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))
        ):
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
        _seen.add(id(value))
        try:
            for index, (k, v) in enumerate(value.items()):
                if index >= MAX_KEYS:
                    out["__truncated_keys__"] = True
                    break
                key = str(k)
                out[key] = sanitize(v, _depth=_depth + 1, _key=key, _seen=_seen)
        finally:
            _seen.discard(id(value))
        return out
    if isinstance(value, (Sequence, set, frozenset)) and not isinstance(value, (str, bytes)):
        items = list(value)[:MAX_ITEMS]
        result: list[Any] = []
        _seen.add(id(value))
        try:
            for item in items:
                # Positional (key, secret) pairs: redact the second element and
                # preserve the key name.
                if (
                    isinstance(item, (tuple, list))
                    and len(item) == 2
                    and isinstance(item[0], str)
                    and is_sensitive_key(item[0])
                ):
                    result.append((item[0], REDACTED))
                else:
                    result.append(
                        sanitize(item, _depth=_depth + 1, _key=_key, _seen=_seen)
                    )
        finally:
            _seen.discard(id(value))
        if len(list(value)) > MAX_ITEMS:
            result.append("[TRUNCATED_ITEMS]")
        if isinstance(value, tuple):
            return tuple(result)
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
