#!/usr/bin/env python3
"""Approval smoke gate for rollout validation.

Creates a harmless approval, extracts approval_id with a strict parser, and
verifies create/status round-trip. No approval is consumed; no secrets are
printed. Exits non-zero on any shape mismatch.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from hermes_mcp_bridge.approval_parser import (
    ApprovalIdParseError,
    extract_approval_id,
    extract_structured_string_field,
)


def _sanitize_approval_id(approval_id: str) -> str:
    prefix = approval_id[:8]
    digest = hashlib.sha256(approval_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}...{digest}"


def _sanitize_parse_error(exc: ApprovalIdParseError) -> dict:
    message = str(exc)
    return {
        "error": message,
        "shape": message,
        "type": type(exc).__name__,
    }


async def _run(url: str) -> int:
    expiry = os.environ.get("HERMES_APPROVAL_SMOKE_EXPIRY", "60")
    try:
        expiry_seconds = int(expiry)
    except ValueError:
        expiry_seconds = 60
    if expiry_seconds <= 0:
        expiry_seconds = 60

    streamable = streamable_http_client(url)
    try:
        read_stream, write_stream, _ = await streamable.__aenter__()
        try:
            session = ClientSession(read_stream, write_stream)
            await session.__aenter__()
            try:
                await session.initialize()

                create_args = {
                    "action": "approval_smoke_test",
                    "resource": "smoke://test/resource",
                    "expires_in_seconds": expiry_seconds,
                    "metadata_sanitized": {
                        "smoke": True,
                        "gate": "rollout-approval-id-parsing",
                    },
                }
                create_result = await session.call_tool(
                    "hermes_approval_create", arguments=create_args
                )
                try:
                    created_id = extract_approval_id(create_result)
                    created_action = extract_structured_string_field(create_result, "action")
                except ApprovalIdParseError as exc:
                    print(
                        json.dumps(
                            _sanitize_parse_error(exc),
                            indent=2,
                        )
                    )
                    return 2

                status_result = await session.call_tool(
                    "hermes_approval_status", arguments={"approval_id": created_id}
                )
                try:
                    status_id = extract_approval_id(status_result)
                    status_action = extract_structured_string_field(status_result, "action")
                    status_decision = extract_structured_string_field(status_result, "decision")
                except ApprovalIdParseError as exc:
                    print(
                        json.dumps(
                            _sanitize_parse_error(exc),
                            indent=2,
                        )
                    )
                    return 3

                if status_id != created_id:
                    print(
                        json.dumps(
                            {
                                "error": "approval_id mismatch",
                                "expected": _sanitize_approval_id(created_id),
                                "got": _sanitize_approval_id(status_id),
                            },
                            indent=2,
                        )
                    )
                    return 4

                if status_action != created_action:
                    print(
                        json.dumps(
                            {
                                "error": "action mismatch",
                                "expected": created_action,
                                "got": status_action,
                            },
                            indent=2,
                        )
                    )
                    return 5

                if status_decision != "requested":
                    print(
                        json.dumps(
                            {
                                "error": "approval not in requested state",
                                "decision": status_decision,
                            },
                            indent=2,
                        )
                    )
                    return 6

                print(
                    json.dumps(
                        {
                            "approval_id": _sanitize_approval_id(created_id),
                            "action": status_action,
                            "decision": status_decision,
                            "expires_in_seconds": expiry_seconds,
                        },
                        indent=2,
                    )
                )
                return 0
            finally:
                await session.__aexit__(None, None, None)
        finally:
            with contextlib.suppress(Exception):
                await streamable.__aexit__(None, None, None)
    except Exception as exc:
        print(
            json.dumps(
                {"error": f"Approval smoke failed: {exc}"},
                indent=2,
                default=str,
            )
        )
        return 10


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.url)))


if __name__ == "__main__":
    main()
