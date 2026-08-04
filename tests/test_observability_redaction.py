"""Redaction tests: nested structures, exceptions, secret-looking strings."""

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

SECRET = "sk-live-ABCDEF1234567890abcdef"


def _dump(value: object) -> str:
    return json.dumps(sanitize(value), sort_keys=True, default=str)


def test_authorization_header_is_redacted() -> None:
    payload = {"headers": {"Authorization": f"Bearer {SECRET}"}}
    dumped = _dump(payload)
    assert SECRET not in dumped
    assert REDACTED in dumped


def test_bearer_inside_free_text_is_redacted() -> None:
    text = f"request failed with Authorization: Bearer {SECRET}"
    scrubbed = redact_text(text)
    assert SECRET not in scrubbed
    assert REDACTED in scrubbed


def test_api_key_assignment_in_string_is_redacted() -> None:
    scrubbed = redact_text(f'api_key="{SECRET}" and password=hunter2')
    assert SECRET not in scrubbed
    assert "hunter2" not in scrubbed


def test_jwt_and_pem_are_redacted() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert jwt not in redact_text(jwt)
    assert "MIIabc" not in redact_text(pem)


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


def test_prompt_and_output_never_appear() -> None:
    payload = {"prompt": "classified prompt body", "output": "classified output body"}
    dumped = _dump(payload)
    assert "classified prompt body" not in dumped
    assert "classified output body" not in dumped
    assert dumped.count(REDACTED) == 2


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


def test_paths_are_reduced() -> None:
    assert redact_path("/var/lib/hermes-mcp-bridge/state.sqlite3") == "[PATH:sqlite3]"
    dumped = _dump({"db_path": "/var/lib/hermes/state.sqlite3"})
    assert "/var/lib" not in dumped


def test_path_like_strings_in_free_text_are_masked() -> None:
    assert "/etc/hermes/secret.env" not in redact_text("read /etc/hermes/secret.env failed")


def test_approval_id_is_fingerprinted_not_full() -> None:
    approval_id = "appr-0123456789abcdef0123456789abcdef"
    result = sanitize({"approval_id": approval_id})
    assert result["approval_id"] != approval_id
    assert approval_id not in json.dumps(result)
    assert result["approval_id"].startswith("appr")


def test_fingerprint_is_stable_and_non_reversible() -> None:
    assert fingerprint("abc123") == fingerprint("abc123")
    assert "abc123" not in fingerprint("abc123")[4:]


def test_depth_and_breadth_limits() -> None:
    deep: dict = {"level": 0}
    node = deep
    for i in range(1, 20):
        node["child"] = {"level": i}
        node = node["child"]
    assert "[MAX_DEPTH]" in _dump(deep)
    assert "[TRUNCATED_ITEMS]" in _dump(list(range(500)))


def test_long_strings_are_truncated() -> None:
    result = sanitize("x" * 5000)
    assert len(result) < 1000
    assert result.endswith("[truncated]")


def test_bytes_are_not_leaked() -> None:
    assert sanitize(b"secret-bytes") == f"[BYTES:{len(b'secret-bytes')}]"


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
