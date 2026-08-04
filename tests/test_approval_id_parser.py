from __future__ import annotations

import json

import pytest

from hermes_mcp_bridge.approval_parser import (
    ApprovalIdParseError,
    extract_approval_id,
    extract_approval_id_from_mcp_result,
)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"approval_id": "app-abc123"}, "app-abc123"),
        ({"result": {"approval_id": "wrap-001"}}, "wrap-001"),
    ],
)
def test_extract_approval_id_accepted_shapes(payload: dict[str, object], expected: str) -> None:
    assert extract_approval_id(payload) == expected


def test_extract_approval_id_structured_content() -> None:
    assert (
        extract_approval_id_from_mcp_result(
            {"structuredContent": {"approval_id": "sc-01"}}
        )
        == "sc-01"
    )


def test_extract_approval_id_structured_content_lower() -> None:
    assert (
        extract_approval_id_from_mcp_result(
            {"structured_content": {"approval_id": "sc-02"}}
        )
        == "sc-02"
    )


def test_extract_approval_id_text_content() -> None:
    class _Item:
        text = json.dumps({"approval_id": "text-01"})

    assert extract_approval_id_from_mcp_result({"content": [_Item()]}) == "text-01"


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


def test_create_status_round_trip_preserves_exact_id() -> None:
    approval_id = "approval-abc123._:-XYZ"

    class _Item:
        text = json.dumps({"approval_id": approval_id})

    create_result = {"content": [_Item()]}
    status_result = {"structuredContent": {"approval_id": approval_id}}

    assert extract_approval_id_from_mcp_result(create_result) == approval_id
    assert extract_approval_id_from_mcp_result(status_result) == approval_id
