#!/usr/bin/env python3
"""Discover and exercise the local Hermes MCP Bridge without exposing secrets."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {
    "hermes_prompt",
    "hermes_status",
    "hermes_stop",
    "hermes_health",
}


def _tool_result_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured

    text_parts: list[str] = []
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            text_parts.append(text)
    if not text_parts:
        return None

    combined = "\n".join(text_parts)
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return combined


async def _run(url: str, prompt: str | None, wait_seconds: float) -> None:
    async with (
        streamable_http_client(url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()

        tool_list = await session.list_tools()
        names = {tool.name for tool in tool_list.tools}
        missing = sorted(EXPECTED_TOOLS - names)
        print(json.dumps({"tools": sorted(names), "missing": missing}, indent=2))
        if missing:
            raise RuntimeError(f"Missing expected MCP tools: {', '.join(missing)}")

        health = await session.call_tool(
            "hermes_health",
            arguments={"detailed": False},
        )
        print(
            json.dumps(
                {"hermes_health": _tool_result_payload(health)},
                indent=2,
                default=str,
            )
        )

        if prompt is not None:
            result = await session.call_tool(
                "hermes_prompt",
                arguments={
                    "prompt": prompt,
                    "wait_seconds": wait_seconds,
                },
            )
            print(
                json.dumps(
                    {"hermes_prompt": _tool_result_payload(result)},
                    indent=2,
                    default=str,
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8765/mcp",
        help="Streamable HTTP MCP endpoint",
    )
    parser.add_argument(
        "--prompt",
        help="Optional read-only prompt to delegate to Hermes",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=180.0,
        help="Maximum bridge-side wait for an optional prompt",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.url, args.prompt, max(0.0, args.wait_seconds)))


if __name__ == "__main__":
    main()
