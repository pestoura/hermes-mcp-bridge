#!/usr/bin/env python3
"""Discover and exercise the local Hermes MCP Bridge without exposing secrets."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {
    "hermes_prompt",
    "hermes_submit",
    "hermes_wait",
    "hermes_status",
    "hermes_stop",
    "hermes_health",
    "hermes_recent_runs",
    "hermes_capabilities",
    "hermes_agent_card",
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
    validation_passed = False
    streamable = streamable_http_client(url)
    try:
        read_stream, write_stream, _ = await streamable.__aenter__()
        try:
            session = ClientSession(read_stream, write_stream)
            await session.__aenter__()
            try:
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
                health_payload = _tool_result_payload(health)
                print(
                    json.dumps(
                        {"hermes_health": health_payload},
                        indent=2,
                        default=str,
                    )
                )
                if not isinstance(health_payload, dict):
                    raise RuntimeError("hermes_health did not return a payload")
                if health_payload.get("upstream", {}).get("status") not in ("healthy", "ok"):
                    raise RuntimeError("Hermes upstream health is not healthy")
                bridge = health_payload.get("bridge") or {}
                if str(bridge.get("state_registry", {}).get("status", "up")) != "up":
                    raise RuntimeError("bridge state_registry health is not up")
                if bridge.get("schema_version") != "0.4.0":
                    raise RuntimeError("bridge schema_version is not 0.4.0")
                if not bridge.get("manifest_version"):
                    raise RuntimeError("bridge manifest_version is missing")
                if not bridge.get("manifest_hash"):
                    raise RuntimeError("bridge manifest_hash is missing")

                capabilities = await session.call_tool(
                    "hermes_capabilities",
                    arguments={},
                )
                capabilities_payload = _tool_result_payload(capabilities)
                print(
                    json.dumps(
                        {"hermes_capabilities": capabilities_payload},
                        indent=2,
                        default=str,
                    )
                )
                if not isinstance(capabilities_payload, dict):
                    raise RuntimeError("hermes_capabilities did not return a payload")
                if capabilities_payload.get("bridge_version") != "0.4.0":
                    raise RuntimeError("capability bridge_version is not 0.4.0")
                if capabilities_payload.get("schema_version") != "0.4.0":
                    raise RuntimeError("capability schema_version is not 0.4.0")
                if not capabilities_payload.get("manifest_hash"):
                    raise RuntimeError("capability manifest_hash is missing")
                if capabilities_payload.get("upstream_capabilities_source") not in (
                    "upstream",
                    "fallback",
                    None,
                ):
                    raise RuntimeError("capability upstream source is unexpected")

                agent_card = await session.call_tool(
                    "hermes_agent_card",
                    arguments={},
                )
                agent_card_payload = _tool_result_payload(agent_card)
                print(
                    json.dumps(
                        {"hermes_agent_card": agent_card_payload},
                        indent=2,
                        default=str,
                    )
                )
                if not isinstance(agent_card_payload, dict):
                    raise RuntimeError("hermes_agent_card did not return a payload")
                if agent_card_payload.get("schema_version") != "0.4.0":
                    raise RuntimeError("agent card schema_version is not 0.4.0")
                if agent_card_payload.get("version") != "0.4.0":
                    raise RuntimeError("agent card version is not 0.4.0")
                if not agent_card_payload.get("card_hash"):
                    raise RuntimeError("agent card card_hash is missing")

                if prompt is not None:

                    async def progress(
                        current: float,
                        total: float | None,
                        message: str | None,
                    ) -> None:
                        print(
                            json.dumps(
                                {
                                    "progress": current,
                                    "total": total,
                                    "message": message,
                                }
                            ),
                            flush=True,
                        )

                    result = await session.call_tool(
                        "hermes_prompt",
                        arguments={
                            "prompt": prompt,
                            "wait_seconds": wait_seconds,
                        },
                        read_timeout_seconds=timedelta(
                            seconds=max(120.0, wait_seconds + 60.0)
                        ),
                        progress_callback=progress,
                    )
                    print(
                        json.dumps(
                            {"hermes_prompt": _tool_result_payload(result)},
                            indent=2,
                            default=str,
                        )
                    )
                validation_passed = True
            finally:
                await session.__aexit__(None, None, None)
        except Exception:
            if not validation_passed:
                raise
            raise
        finally:
            try:
                await streamable.__aexit__(None, None, None)
            except Exception as cleanup_exc:
                if validation_passed:
                    print(
                        json.dumps(
                            {
                                "smoke_lifecycle_warning": str(cleanup_exc),
                                "note": "smoke validation passed; shutdown error treated as benign",
                            },
                            indent=2,
                            default=str,
                        )
                    )
                elif sys.exc_info()[0] is not None:
                    pass
                else:
                    raise
    except Exception:
        if not validation_passed:
            raise


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
        help="Maximum connected wait for an optional prompt",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.url, args.prompt, max(0.0, args.wait_seconds)))


if __name__ == "__main__":
    main()
