from __future__ import annotations

import json

import pytest

from hermes_mcp_bridge.approval_parser import (
    ApprovalIdParseError,
    extract_approval_id,
    extract_approval_id_from_mcp_result,
    extract_structured_string_field,
)


class _FakeContentItem:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMcpResult:
    def __init__(
        self,
        structuredContent: dict[str, str] | None = None,
        content: list[_FakeContentItem] | None = None,
    ) -> None:
        self.structuredContent = structuredContent
        self.content = content


# --- accepted shapes ---


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"approval_id": "app-abc123"}, "app-abc123"),
        ({"result": {"approval_id": "wrap-001"}}, "wrap-001"),
    ],
)
def test_extract_approval_id_accepted_shapes(payload: dict[str, object], expected: str) -> None:
    assert extract_approval_id(payload) == expected


# --- structured wrappers ---


def test_extract_approval_id_structured_content() -> None:
    assert (
        extract_approval_id_from_mcp_result({"structuredContent": {"approval_id": "sc-01"}})
        == "sc-01"
    )


def test_extract_approval_id_structured_content_lower() -> None:
    assert (
        extract_approval_id_from_mcp_result({"structured_content": {"approval_id": "sc-02"}})
        == "sc-02"
    )


# --- text content fallback ---


def test_extract_approval_id_text_content() -> None:
    assert (
        extract_approval_id_from_mcp_result(
            {"content": [_FakeContentItem(text=json.dumps({"approval_id": "text-01"}))]}
        )
        == "text-01"
    )


# --- rejections ---


def test_extract_approval_id_rejects_quoted_id() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"approval_id": '"quoted-id"'})


def test_extract_approval_id_rejects_whitespace() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"approval_id": " spaced "})


def test_extract_approval_id_rejects_newline() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"approval_id": "line1\nline2"})


def test_extract_approval_id_rejects_dict_value() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"approval_id": {"nested": True}})


def test_extract_approval_id_rejects_list_value() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"approval_id": ["a", "b"]})


def test_extract_approval_id_rejects_non_string_value() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"approval_id": 123})


def test_extract_approval_id_rejects_invalid_chars() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"approval_id": "invalid spaces"})


def test_extract_approval_id_from_mcp_result_rejects_raw_string() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id_from_mcp_result("raw-string-payload")


# --- top-level / nested lists ---


def test_extract_approval_id_rejects_top_level_list() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id([{"approval_id": "a"}])


def test_extract_approval_id_rejects_top_level_tuple() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id(({"approval_id": "a"},))


def test_extract_approval_id_rejects_nested_list_payload() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"result": [{"approval_id": "a"}]})


def test_extract_approval_id_rejects_approval_id_list_value() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"approval_id": [{"nested": True}]})


# --- ambiguous / multiple wrappers ---


def test_extract_approval_id_rejects_double_wrapper() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"result": {"result": {"approval_id": "x"}}})


def test_extract_approval_id_rejects_wrapper_then_structured_content() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_approval_id({"result": {"structuredContent": {"approval_id": "x"}}})


# --- structured string fields ---


def test_extract_structured_string_field_action_and_decision() -> None:
    payload = {
        "structuredContent": {
            "approval_id": "req-123",
            "action": "scan",
            "decision": "requested",
        }
    }
    assert extract_structured_string_field(payload, "action") == "scan"
    assert extract_structured_string_field(payload, "decision") == "requested"


def test_extract_structured_string_field_rejects_non_dict_wrapper() -> None:
    with pytest.raises(ApprovalIdParseError):
        extract_structured_string_field([{"action": "x"}], "action")


# --- round-trip fake MCP objects ---


def test_fake_mcp_result_round_trip_exact_id() -> None:
    approval_id = "approval-abc123._:-XYZ"
    create_result = _FakeMcpResult(
        content=[_FakeContentItem(text=json.dumps({"approval_id": approval_id}))]
    )
    status_result = _FakeMcpResult(
        structuredContent={
            "approval_id": approval_id,
            "action": "approval_smoke_test",
            "decision": "requested",
        }
    )

    assert extract_approval_id_from_mcp_result(create_result) == approval_id
    assert extract_approval_id_from_mcp_result(status_result) == approval_id


def test_fake_mcp_result_structured_content_lower() -> None:
    approval_id = "status-001"
    result = _FakeMcpResult(structuredContent={"approval_id": approval_id})
    assert extract_approval_id_from_mcp_result(result) == approval_id


# --- error sanitization ---


def test_parse_error_message_excludes_raw_payload() -> None:
    payload = {"approval_id": ["secret"]}
    try:
        extract_approval_id(payload)
    except ApprovalIdParseError as exc:
        message = str(exc)
        assert "['secret']" not in message
        assert "secret" not in message
        return
    pytest.fail("ApprovalIdParseError not raised")
