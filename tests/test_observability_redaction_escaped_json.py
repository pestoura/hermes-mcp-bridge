"""Adversarial regression tests for the two final HIGH redactor findings.

HIGH-1: JSON embedded in free text must be redacted at escape level 0, 1 and 2
        (``"key"``, ``\\"key\\"``, ``\\\\"key\\\\"``) without any destructive
        global unescape. The original escape structure is preserved verbatim
        and only the bound value is replaced with ``[REDACTED]``.

HIGH-2: Bare or malformed Cookie/Set-Cookie headers (no analysable
        ``name=value`` pairs) must fail closed and redact the whole value,
        while well-formed cookies keep their benign attributes.

Both properties must hold in ``redact_text``, ``sanitize``, ``log_event``,
``JsonFormatter`` extras and exception messages, must stay idempotent across
six passes, must never emit ``]]``, and must leave benign text intact.
"""

from __future__ import annotations

import json
import logging

import pytest

from hermes_mcp_bridge.observability.logging import (
    JsonFormatter,
    configure_logging,
    log_event,
)
from hermes_mcp_bridge.observability.redaction import (
    REDACTED,
    redact_text,
    sanitize,
)

# Exact audit payloads -------------------------------------------------------

SECRET_VALUE = "sk-live-AAAABBBBCCCC1111"

# Escape level 0: plain JSON.
ESC0_CASES: list[tuple[str, str]] = [
    ('{"api_key": "sk-live-AAAABBBBCCCC1111"}', SECRET_VALUE),
    ('{"token":"ghp_AAAABBBBCCCCDDDD"}', "ghp_AAAABBBBCCCCDDDD"),
    ('{"password": "hunter2secret"}', "hunter2secret"),
    ('{"client_secret":"csXYZ9876543210"}', "csXYZ9876543210"),
    ('{"Authorization": "Bearer abcdefghijk"}', "abcdefghijk"),
    ("{'api_key': 'sk-live-AAAABBBBCCCC1111'}", SECRET_VALUE),
]

# Escape level 1: JSON serialized once into a string field.
ESC1_CASES: list[tuple[str, str]] = [
    ('{\\"api_key\\": \\"sk-live-AAAABBBBCCCC1111\\"}', SECRET_VALUE),
    ('payload=\\"token\\":\\"abcdef123456\\" done', "abcdef123456"),
    ('\\"password\\" = \\"hunter2secret\\"', "hunter2secret"),
    ('\\"client_secret\\": \\"csXYZ9876543210\\"', "csXYZ9876543210"),
    ('\\"Authorization\\": \\"Bearer abcdefghijk\\"', "abcdefghijk"),
    ('\\"cookie\\": \\"sessionid=zzzz9999\\"', "zzzz9999"),
]

# Escape level 2: JSON serialized twice.
ESC2_CASES: list[tuple[str, str]] = [
    ('{\\\\"api_key\\\\": \\\\"sk-live-AAAABBBBCCCC1111\\\\"}', SECRET_VALUE),
    ('{\\\\"token\\\\":\\\\"ghp_AAAABBBBCCCCDDDD\\\\"}', "ghp_AAAABBBBCCCCDDDD"),
    ('{\\\\"password\\\\": \\\\"hunter2secret\\\\"}', "hunter2secret"),
    ('{\\\\"client_secret\\\\": \\\\"csXYZ9876543210\\\\"}', "csXYZ9876543210"),
]

ALL_ESCAPE_CASES = ESC0_CASES + ESC1_CASES + ESC2_CASES


@pytest.mark.parametrize("raw,secret", ALL_ESCAPE_CASES)
def test_escaped_json_secret_is_redacted(raw: str, secret: str) -> None:
    out = redact_text(raw)
    assert secret not in out, f"{secret!r} leaked from {raw!r} -> {out!r}"
    assert REDACTED in out


@pytest.mark.parametrize("raw,secret", ALL_ESCAPE_CASES)
def test_escaped_json_structure_preserved(raw: str, secret: str) -> None:
    """No destructive global unescape: escape level is preserved verbatim."""

    out = redact_text(raw)
    # Backslash count must not shrink -- an unescape would destroy them.
    assert out.count("\\") == raw.count("\\"), f"escape level altered: {out!r}"
    # Structural delimiters survive.
    for char in ("{", "}"):
        assert out.count(char) == raw.count(char)


@pytest.mark.parametrize("raw,secret", ALL_ESCAPE_CASES)
def test_escaped_json_idempotent_six_passes(raw: str, secret: str) -> None:
    current = redact_text(raw)
    first = current
    for _ in range(5):
        current = redact_text(current)
    assert current == first
    assert "]]" not in current
    assert secret not in current


@pytest.mark.parametrize("raw,secret", ALL_ESCAPE_CASES)
def test_escaped_json_via_sanitize(raw: str, secret: str) -> None:
    out = json.dumps(sanitize({"payload": raw}), default=str)
    assert secret not in out
    assert REDACTED in out


@pytest.mark.parametrize("raw,secret", ALL_ESCAPE_CASES)
def test_escaped_json_in_exception_message(raw: str, secret: str) -> None:
    err = RuntimeError(f"upstream rejected body {raw}")
    out = json.dumps(sanitize({"error": err}), default=str)
    assert secret not in out
    assert REDACTED in out


# --- Bare / malformed cookies must fail closed (HIGH-2) ---------------------

BARE_COOKIE_CASES: list[str] = [
    "Cookie: abcDEF123456789",
    "cookie=abcDEF123456789",
    "Set-Cookie: abcDEF123456789",
    "set-cookie: abcDEF123456789",
    "SET-COOKIE: abcDEF123456789",
    "COOKIE:\tabcDEF123456789",
    "Cookie:\t\tabcDEF123456789",
    "Cookie:    abcDEF123456789",
    "  cookie :   abcDEF123456789  ",
    "Set-Cookie: bad value here",
    "Cookie: ; ; ",
    "Cookie: =novalue",
    "Cookie: sessiondata_without_equals_sign",
]

BARE_COOKIE_SECRET = "abcDEF123456789"


@pytest.mark.parametrize("raw", BARE_COOKIE_CASES)
def test_bare_cookie_fails_closed(raw: str) -> None:
    out = redact_text(raw)
    assert BARE_COOKIE_SECRET not in out, f"bare cookie leaked: {out!r}"
    assert "novalue" not in out
    assert "sessiondata_without_equals_sign" not in out
    assert REDACTED in out


@pytest.mark.parametrize("raw", BARE_COOKIE_CASES)
def test_bare_cookie_idempotent_six_passes(raw: str) -> None:
    current = redact_text(raw)
    first = current
    for _ in range(5):
        current = redact_text(current)
    assert current == first
    assert "]]" not in current
    assert BARE_COOKIE_SECRET not in current


@pytest.mark.parametrize("raw", BARE_COOKIE_CASES)
def test_bare_cookie_via_sanitize_and_logging(raw: str) -> None:
    out = json.dumps(sanitize({"note": raw}), default=str)
    assert BARE_COOKIE_SECRET not in out
    assert REDACTED in out


def test_bare_cookie_multiline_does_not_leak_following_line() -> None:
    raw = "Cookie: abcDEF123456789\nnext line run_id=ok benign"
    out = redact_text(raw)
    assert BARE_COOKIE_SECRET not in out
    assert "run_id=ok" in out
    assert "benign" in out


def test_bare_cookie_then_benign_text_preserved() -> None:
    raw = "Set-Cookie: abcDEF123456789\nstatus=completed duration_ms=42"
    out = redact_text(raw)
    assert BARE_COOKIE_SECRET not in out
    assert "status=completed" in out
    assert "duration_ms=42" in out


# --- Well-formed cookies keep their benign attributes -----------------------


def test_valid_cookie_preserves_all_benign_attributes() -> None:
    raw = (
        "Set-Cookie: sessionid=secret9999; csrf=z3k2; lang=pt; theme=dark; "
        "locale=pt_PT; utm_source=news; utm_medium=email; utm_campaign=q3; "
        "Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=3600; "
        "Expires=Wed, 09 Jun 2027 10:18:14 GMT; Domain=.example.com"
    )
    out = redact_text(raw)
    assert "secret9999" not in out
    assert "z3k2" not in out
    assert "sessionid=[REDACTED]" in out
    assert "csrf=[REDACTED]" in out
    for benign in (
        "lang=pt",
        "theme=dark",
        "locale=pt_PT",
        "utm_source=news",
        "utm_medium=email",
        "utm_campaign=q3",
        "Path=/",
        "HttpOnly",
        "Secure",
        "SameSite=Lax",
        "Max-Age=3600",
        "Expires=Wed, 09 Jun 2027 10:18:14 GMT",
        "Domain=.example.com",
    ):
        assert benign in out, f"benign attribute lost: {benign}"


def test_valid_cookie_idempotent_six_passes() -> None:
    raw = "Set-Cookie: sid=secret9999; lang=pt; Path=/; HttpOnly; SameSite=Lax"
    current = redact_text(raw)
    first = current
    for _ in range(5):
        current = redact_text(current)
    assert current == first
    assert "]]" not in current
    assert "secret9999" not in current
    assert "lang=pt" in current


# --- End-to-end logging / formatter -----------------------------------------


def test_log_event_redacts_escaped_json_and_bare_cookie() -> None:
    # ``configure_logging`` binds its stderr handler once, at first call, so
    # neither caplog (propagation is disabled) nor capfd reliably observe it.
    # Attach a dedicated in-memory sink with the real JsonFormatter instead.
    import io

    configure_logging()
    logger = logging.getLogger("hermes_mcp_bridge")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        log_event(
            "upstream.error",
            payload='{\\"api_key\\": \\"sk-live-AAAABBBBCCCC1111\\"}',
            header="Cookie: abcDEF123456789",
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    blob = stream.getvalue()
    assert "upstream.error" in blob, "no log output captured"
    assert SECRET_VALUE not in blob
    assert BARE_COOKIE_SECRET not in blob
    assert REDACTED in blob
    assert "]]" not in blob


def test_json_formatter_extras_redacts_both_findings() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="bridge",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request failed",
        args=(),
        exc_info=None,
    )
    record.payload = '{\\\\"client_secret\\\\": \\\\"csXYZ9876543210\\\\"}'
    record.header = "set-cookie:\tabcDEF123456789"
    out = formatter.format(record)
    assert "csXYZ9876543210" not in out
    assert BARE_COOKIE_SECRET not in out
    assert REDACTED in out
    assert "]]" not in out


def test_exception_end_to_end_with_bare_cookie() -> None:
    err = ValueError("auth failed for Cookie: abcDEF123456789")
    out = json.dumps(sanitize({"error": err}), default=str)
    assert BARE_COOKIE_SECRET not in out
    assert REDACTED in out


# --- Combined / mixed adversarial payloads ----------------------------------


def test_two_secrets_same_line_escaped_and_cookie() -> None:
    raw = '\\"api_key\\": \\"sk-live-AAAABBBBCCCC1111\\" and Cookie: abcDEF123456789'
    out = redact_text(raw)
    assert SECRET_VALUE not in out
    assert BARE_COOKIE_SECRET not in out
    assert out.count(REDACTED) >= 2


def test_two_escaped_secrets_same_line() -> None:
    raw = '\\"token\\":\\"tok111111\\", \\"password\\":\\"pw2222222\\"'
    out = redact_text(raw)
    assert "tok111111" not in out
    assert "pw2222222" not in out
    assert out.count(REDACTED) == 2


def test_multiline_mixed_escape_levels() -> None:
    raw = (
        '{"api_key": "sk-live-AAAABBBBCCCC1111"}\n'
        '{\\"token\\": \\"tok111111\\"}\n'
        '{\\\\"password\\\\": \\\\"pw2222222\\\\"}\n'
        "run_id=abc-123 sha256=deadbeef temperature=0.7"
    )
    out = redact_text(raw)
    assert SECRET_VALUE not in out
    assert "tok111111" not in out
    assert "pw2222222" not in out
    # Benign trailing content survives untouched.
    assert "run_id=abc-123" in out
    assert "sha256=deadbeef" in out
    assert "temperature=0.7" in out


BENIGN_TEXTS: list[str] = [
    "run_id=abc-123 session_id=def-456",
    "sha256=deadbeef uuid=a-b-c-d",
    "temperature=0.7 top_p=0.95",
    "status=completed duration_ms=42 outcome=success",
    "Path=/ and SameSite=Lax preserved",
    "the cookie jar was empty",
    "no secrets in this sentence at all",
]


@pytest.mark.parametrize("raw", BENIGN_TEXTS)
def test_benign_text_untouched(raw: str) -> None:
    out = redact_text(raw)
    assert REDACTED not in out, f"false positive redaction on {raw!r} -> {out!r}"
    assert out == raw


@pytest.mark.parametrize("raw", BENIGN_TEXTS)
def test_benign_text_idempotent_six_passes(raw: str) -> None:
    current = raw
    for _ in range(6):
        current = redact_text(current)
    assert current == raw
