"""Redaction tests: nested structures, exceptions, secret-looking strings.

Covers the audit variants: Authorization/Proxy-Authorization with and without
scheme (Bearer/Basic/Digest/token), in ':' and '=' forms, Cookie/Set-Cookie
multi-pair in free text, embedded secrets bound to sensitive names (with no
leaked suffix), positional tuple/list pairs, bytes, exceptions, cycles, depth
limits, and benign-hash preservation.

The redactor output is canonical and idempotent: sanitize(sanitize(x)) == sanitize(x)
and re-running on already-redacted free text never produces ']]' or other
doubled closers.
"""

from __future__ import annotations

import json

import pytest

from hermes_mcp_bridge.observability.redaction import (
    REDACTED,
    enforce_total_size,
    fingerprint,
    redact_path,
    redact_text,
    sanitize,
)

SECRET = "sk-super-secret-value-1234567890"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6tyaCavwOLdHQnf1rFxE"
    "-----END RSA PRIVATE KEY-----"
)


def _dump(value: object) -> str:
    return json.dumps(sanitize(value), sort_keys=True, default=str)


# --- Header variants ----------------------------------------------------------


def test_authorization_bearer_value_redacted() -> None:
    payload = {"headers": {"Authorization": f"Bearer {SECRET}"}}
    dumped = _dump(payload)
    assert SECRET not in dumped
    assert REDACTED in dumped


def test_authorization_basic_value_redacted() -> None:
    dumped = _dump({"headers": {"Authorization": f"Basic {SECRET}"}})
    assert SECRET not in dumped
    assert REDACTED in dumped


def test_authorization_digest_value_redacted() -> None:
    dumped = _dump({"headers": {"Authorization": f"Digest {SECRET}"}})
    assert SECRET not in dumped
    assert REDACTED in dumped


def test_authorization_scheme_token_no_prefix_redacted() -> None:
    dumped = _dump({"headers": {"Authorization": SECRET}})
    assert SECRET not in dumped
    assert REDACTED in dumped


def test_proxy_authorization_redacted() -> None:
    dumped = _dump({"headers": {"Proxy-Authorization": f"Digest {SECRET}"}})
    assert SECRET not in dumped
    assert REDACTED in dumped


def test_authorization_colon_form_in_text() -> None:
    text = "upstream rejected: Authorization: Bearer " + SECRET
    scrubbed = redact_text(text)
    assert SECRET not in scrubbed
    assert "Authorization [REDACTED]" in scrubbed


def test_authorization_equals_form_in_text() -> None:
    text = "request headers Authorization=" + SECRET + " sent"
    scrubbed = redact_text(text)
    assert SECRET not in scrubbed
    assert "Authorization [REDACTED]" in scrubbed


def test_bearer_inside_free_text_is_redacted() -> None:
    text = "request failed with Authorization: Bearer " + SECRET
    scrubbed = redact_text(text)
    assert "Authorization [REDACTED]" in scrubbed
    assert "]]" not in scrubbed


def test_redact_text_is_idempotent() -> None:
    text = "Authorization: Bearer " + SECRET + " and Cookie: session=" + SECRET
    once = redact_text(text)
    twice = redact_text(once)
    assert once == twice
    assert "]]" not in twice


# --- Cookies ------------------------------------------------------------------


def test_cookie_in_text_redacted_multi_pair() -> None:
    text = "Set-Cookie: session=" + SECRET + "; csrf=x7y9; lang=pt"
    scrubbed = redact_text(text)
    assert SECRET not in scrubbed
    assert "session=[REDACTED]" in scrubbed
    assert "csrf=[REDACTED]" in scrubbed
    assert "lang=pt" in scrubbed  # benign value preserved


def test_cookie_in_dict_redacted() -> None:
    dumped = _dump({"headers": {"Cookie": f"auth={SECRET}; tracking=abc"}})
    assert SECRET not in dumped
    # Whole Cookie value is redacted at key level (fail closed).
    assert "[REDACTED]" in dumped


def test_set_cookie_header_name_redacted() -> None:
    dumped = _dump({"headers": {"Set-Cookie": f"token={SECRET}"}})
    assert SECRET not in dumped
    assert "[REDACTED]" in dumped


# --- Embedded secrets bound to sensitive names --------------------------------


def test_api_key_assignment_in_string_is_redacted() -> None:
    scrubbed = redact_text(f'api_key="{SECRET}" and password=hunter2')
    assert SECRET not in scrubbed
    assert "hunter2" not in scrubbed
    assert 'api_key="[REDACTED]"' in scrubbed


def test_embedded_secret_no_suffix_leak() -> None:
    dumped = _dump({"config": {"token": SECRET, "secret": SECRET, "api_key": SECRET}})
    assert SECRET not in dumped
    # No partial/suffix leak: value replaced wholly.
    assert dumped.count(REDACTED) == 3


def test_jwt_and_pem_are_redacted() -> None:
    assert JWT not in redact_text(JWT)
    assert "MIIBO" not in redact_text(PEM)
    assert "BEGIN RSA PRIVATE KEY" not in redact_text(PEM)


# --- Positional / sequence pairs ---------------------------------------------


def test_positional_tuple_pair_redacted() -> None:
    payload = {"pairs": [("api_key", SECRET), ("token", SECRET)]}
    dumped = _dump(payload)
    assert SECRET not in dumped
    assert '"api_key": "[REDACTED]"' in dumped or '["api_key", "[REDACTED]"]' in dumped


def test_sequence_of_pairs_redacted() -> None:
    payload = [("password", SECRET), ("username", "pedro")]
    dumped = _dump(payload)
    assert SECRET not in dumped
    assert "pedro" in dumped  # non-secret preserved


# --- Prompt/output ------------------------------------------------------------


def test_prompt_and_output_never_appear() -> None:
    payload = {"prompt": "classified prompt body", "output": "classified output body"}
    dumped = _dump(payload)
    assert "classified prompt body" not in dumped
    assert "classified output body" not in dumped
    assert dumped.count(REDACTED) == 2


# --- Nested / exceptions / structures ---------------------------------------


def test_nested_dict_and_list_are_sanitized() -> None:
    payload = {
        "outer": [
            {"token": SECRET},
            {"safe": "value", "inner": {"password": "x", "prompt": "top secret prompt"}},
        ]
    }
    dumped = _dump(payload)
    assert SECRET not in dumped
    assert "top secret prompt" not in dumped
    assert "value" in dumped


def test_exception_is_reduced_to_type_and_redacted_message() -> None:
    exc = RuntimeError(f"failed calling upstream with Bearer {SECRET}")
    result = sanitize(exc)
    assert result["type"] == "RuntimeError"
    assert SECRET not in result["message"]
    assert "traceback" not in result


def test_exception_inside_container_is_sanitized() -> None:
    payload = {"errors": [ValueError(f"token={SECRET}")]}
    dumped = _dump(payload)
    assert SECRET not in dumped


def test_arbitrary_objects_are_not_repr_serialized() -> None:
    class Leaky:
        def __repr__(self) -> str:  # pragma: no cover - must never be called
            return f"Leaky(secret={SECRET})"

        def __str__(self) -> str:  # pragma: no cover - must never be called
            return f"Leaky(secret={SECRET})"

    dumped = _dump({"obj": Leaky()})
    assert SECRET not in dumped
    assert "[Leaky]" in dumped


def test_bytes_are_not_leaked() -> None:
    assert sanitize(b"secret-bytes") == f"[BYTES:{len(b'secret-bytes')}]"


def test_paths_are_reduced() -> None:
    assert redact_path("/var/lib/hermes-mcp-bridge/state.sqlite3") == "[PATH:sqlite3]"
    dumped = _dump({"db_path": "/var/lib/hermes/state.sqlite3"})
    assert "/var/lib" not in dumped


def test_path_like_strings_in_free_text_are_masked() -> None:
    assert "/etc/hermes/secret.env" not in redact_text("read /etc/hermes/secret.env failed")


# --- Benign hashes preserved --------------------------------------------------


def test_benign_sha256_preserved() -> None:
    digest = "a" * 64
    assert digest in redact_text(f"hash={digest}")


def test_benign_short_hash_preserved() -> None:
    assert "abc123" in redact_text("build abc123 completed")


# --- Approval id fingerprinting ----------------------------------------------


def test_approval_id_is_fingerprinted_not_full() -> None:
    approval_id = "appr-0123456789abcdef0123456789abcdef"
    result = sanitize({"approval_id": approval_id})
    assert result["approval_id"] != approval_id
    assert approval_id not in json.dumps(result)
    assert result["approval_id"].startswith("appr")


def test_fingerprint_is_stable_and_non_reversible() -> None:
    assert fingerprint("abc123") == fingerprint("abc123")
    assert "abc123" not in fingerprint("abc123")[4:]


# --- Limits / cycles ----------------------------------------------------------


def test_depth_and_breadth_limits() -> None:
    deep: dict = {"level": 0}
    node = deep
    for i in range(1, 25):
        node["child"] = {"level": i}
        node = node["child"]
    assert "[MAX_DEPTH]" in _dump(deep)
    assert "[TRUNCATED_ITEMS]" in _dump(list(range(500)))


def test_long_strings_are_truncated() -> None:
    result = sanitize("x" * 5000)
    assert len(result) < 1000
    assert result.endswith("[truncated]")


def test_cycles_do_not_infinite_loop() -> None:
    cycle: dict = {}
    cycle["self"] = cycle
    result = _dump(cycle)
    assert "[CYCLE]" in result


def test_configurable_extra_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_LOG_REDACT_FIELDS", "tenant_ref,custom_field")
    result = sanitize({"custom_field": "sensitive", "other": "kept"})
    assert result["custom_field"] == REDACTED
    assert result["other"] == "kept"


def test_enforce_total_size_keeps_essential_fields() -> None:
    payload = {
        "ts": "2026-01-01T00:00:00Z",
        "level": "INFO",
        "event": "bridge.tool.call",
        "outcome": "success",
        "duration_ms": 1.0,
        "blob": "y" * 20000,
    }
    trimmed = enforce_total_size(payload, max_chars=500)
    assert trimmed["event"] == "bridge.tool.call"
    assert trimmed["outcome"] == "success"
    assert "blob" not in trimmed


# --- Adversarial HIGH-1 / HIGH-2 regression cases ---

ADVERSARIAL_CASES: list[str] = [
    '{"api_key":"sk-secret-123"}',
    '{"api_key": "sk-secret-123"}',
    "{'api_key': 'sk-secret-123'}",
    '{"token": "ghp_secretvalue"}',
    "{'token': 'ghp_secretvalue'}",
    '{"client_secret":"topsecret"}',
    '{"Authorization": "Bearer abc123def456"}',
    '{"Authorization":"abc123def456"}',
    "Proxy-Authorization: Digest xyz789",
    "proxy-authorization=Basic dXNlcjpwYXNz",
    '{"password":"hunter2"}',
    "password=hunter2",
    "access_token=at-12345",
    "refresh_token=rt-999",
    "private_key=MIIabc",
    "Authorization: Bearer tok1",
    "Set-Cookie: session=sk-x7y9; csrf=z3k2; lang=pt; Path=/; HttpOnly",
    "Cookie: auth=sk-z; theme=dark",
    '{"error":"upstream 500","body":"{\\"api_key\\":\\"x\\"}"}',
    "two secrets on same line: token=aaa and password=bbb",
    '{"run_id":"abc-123","session_id":"def-456","sha256":"deadbeef","uuid":"a-b-c-d"}',
    "temperature=0.7",
    "Path=/ and SameSite=Lax preserved",
]


@pytest.mark.parametrize("raw", ADVERSARIAL_CASES)
def test_redact_text_adversarial_cases(raw: str) -> None:
    out = redact_text(raw)
    # Every secret-shaped value must be gone.
    for secret in (
        "sk-secret-123",
        "ghp_secretvalue",
        "topsecret",
        "abc123def456",
        "xyz789",
        "hunter2",
        "at-12345",
        "rt-999",
        "MIIabc",
        "tok1",
        "sk-x7y9",
        "z3k2",
        "sk-z",
        "leaked",
        "aaa",
        "bbb",
    ):
        if secret in raw:
            assert secret not in out, f"{secret} leaked in {raw!r}"
    # Benign identifiers must survive only when present in the source.
    for benign in (
        "abc-123",
        "def-456",
        "deadbeef",
        "a-b-c-d",
        "0.7",
        "lang=pt",
        "Path=/",
        "HttpOnly",
        "Secure",
        "SameSite=Lax",
        "Max-Age=3600",
        "theme=dark",
    ):
        if benign in raw:
            assert benign in out, f"benign {benign} lost from {raw!r}"


@pytest.mark.parametrize("raw", ADVERSARIAL_CASES)
def test_redact_text_is_idempotent_over_three_passes(raw: str) -> None:
    once = redact_text(raw)
    twice = redact_text(once)
    thrice = redact_text(twice)
    assert once == twice == thrice
    assert "]]" not in once
    assert "]]]" not in once
    assert "[REDACTED]=[REDACTED]" not in once


def test_quoted_json_keys_redacted_even_with_scheme() -> None:
    out = redact_text('{"Authorization": "Bearer abc123def456"}')
    assert out == '{"Authorization": "Bearer [REDACTED]"}'


def test_authorization_without_scheme_redacted() -> None:
    out = redact_text('{"Authorization": "abc123def456"}')
    assert "abc123def456" not in out
    assert "[REDACTED]" in out


def test_cookie_preserves_benign_attributes() -> None:
    raw = (
        "Set-Cookie: session=sk-x7y9; csrf=z3k2; lang=SK; Path=/; "
        "HttpOnly; Secure; SameSite=Strict; Max-Age=3600; Expires=Wed, 09 Jun; "
        "Domain=.example.com"
    )
    out = redact_text(raw)
    assert "sk-x7y9" not in out
    assert "z3k2" not in out
    assert "session=[REDACTED]" in out
    assert "csrf=[REDACTED]" in out
    assert "lang=SK" in out
    assert "Path=/" in out
    assert "HttpOnly" in out
    assert "Secure" in out
    assert "SameSite=Strict" in out
    assert "Max-Age=3600" in out
    assert "Expires=Wed, 09 Jun" in out
    assert "Domain=.example.com" in out


def test_exception_with_json_body_is_redacted() -> None:
    class _UpstreamError(Exception):
        def __init__(self, body: str) -> None:
            super().__init__("upstream 500")
            self.body = body

    err = _UpstreamError('{"api_key":"leaked"}')
    # The embedded JSON body must be scrubbed when serialized.
    out = redact_text(err.body)
    assert "leaked" not in out
    assert "[REDACTED]" in out


def test_logging_extras_with_json_string_is_redacted() -> None:
    extras = {"payload": '{"password":"hunter2"}'}
    scrubbed = sanitize(extras)
    assert "hunter2" not in str(scrubbed)
    assert "[REDACTED]" in str(scrubbed)


def test_two_secrets_same_line_both_redacted() -> None:
    out = redact_text("token=aaa and password=bbb")
    assert "aaa" not in out
    assert "bbb" not in out
    assert out.count("[REDACTED]") == 2


def test_multiline_secret_does_not_leak_next_line() -> None:
    raw = 'line1 {"api_key":"sk-x"}\nline2 benign run_id=ok'
    out = redact_text(raw)
    assert "sk-x" not in out
    assert "run_id=ok" in out


def test_sanitize_is_idempotent_over_three_passes() -> None:
    payload = {
        "api_key": "sk-x",
        "nested": {"token": "ghp-y"},
        "list": [{"password": "z"}],
    }
    once = sanitize(payload)
    twice = sanitize(once)
    thrice = sanitize(twice)
    assert once == twice == thrice
    assert "sk-x" not in str(once)
    assert "ghp-y" not in str(once)
    assert "z" not in str(once)


def test_redacted_placeholder_never_re_redacted() -> None:
    for base in ["token=[REDACTED]", "Authorization [REDACTED]", '{"api_key":"[REDACTED]"}']:
        out = redact_text(base)
        assert out == base
        assert "[REDACTED]=[REDACTED]" not in out
        assert out.count("[REDACTED]") == base.count("[REDACTED]")
