"""Phase 2 shadow-output hardening.

Covers the acceptance-only ``HERMES_V2_ACCEPTANCE_STRICT_JSON`` opt-in in the
upstream run client, the strict connected-acceptance answer parser, the
unchanged 27-tool contract, and the launcher wiring.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from itertools import product
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from hermes_mcp_bridge import client_base
from hermes_mcp_bridge.client import HermesClient
from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.contracts import expected_tool_count, required_tools
from hermes_mcp_bridge.protocol import OrchestrationMode

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "v2_phase2_connected_jarvas.sh"
COLLECTOR_PATH = ROOT / "scripts" / "v2_phase2_direct_read_acceptance.py"

ENV_FLAG = "HERMES_V2_ACCEPTANCE_STRICT_JSON"
EXPECTED_INSTRUCTION = (
    "Return exactly the JSON shape requested by the user prompt, preserving "
    "the exact key names requested. Emit only that JSON: no prose, no "
    "explanation, no code fence and no wrapper object."
)


def _load_collector() -> Any:
    spec = importlib.util.spec_from_file_location(
        "v2_phase2_direct_read_acceptance_shadow_output", COLLECTOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = _load_collector()


def _settings() -> Settings:
    return Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        hermes_run_poll_interval_seconds=0.001,
        hermes_run_max_wait_seconds=0.1,
    )


_COMBINATIONS = [
    (agent, subagents, orchestration)
    for agent, subagents, orchestration in product(
        [None, "infra"],
        [None, [], ["security"], ["security", "platform"]],
        list(OrchestrationMode),
    )
]


async def _capture_payload(
    *,
    agent: str | None,
    subagents: list[str] | None,
    orchestration: OrchestrationMode,
) -> bytes:
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/runs":
            captured["body"] = bytes(request.content)
            return httpx.Response(202, json={"run_id": "run-1", "status": "completed"})
        if request.method == "GET" and request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"object": "list", "data": []})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = HermesClient(_settings(), transport_factory=lambda: httpx.MockTransport(handler))
    await client.create_run(
        prompt="Read only task",
        session_id="session-1",
        agent=agent,
        subagents=subagents,
        orchestration=orchestration,
    )
    return captured["body"]


# ---------------------------------------------------------------------------
# (2) flag off: byte parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag_value", [None, "", "0", "true", "yes", "01", " 1", "1 ", "2"])
@pytest.mark.parametrize("agent,subagents,orchestration", _COMBINATIONS)
@pytest.mark.asyncio
async def test_payload_is_byte_identical_when_flag_is_not_exactly_one(
    monkeypatch: pytest.MonkeyPatch,
    flag_value: str | None,
    agent: str | None,
    subagents: list[str] | None,
    orchestration: OrchestrationMode,
) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    baseline = await _capture_payload(agent=agent, subagents=subagents, orchestration=orchestration)

    if flag_value is None:
        monkeypatch.delenv(ENV_FLAG, raising=False)
    else:
        monkeypatch.setenv(ENV_FLAG, flag_value)

    candidate = await _capture_payload(
        agent=agent, subagents=subagents, orchestration=orchestration
    )
    assert candidate == baseline
    assert EXPECTED_INSTRUCTION.encode("utf-8") not in candidate


# ---------------------------------------------------------------------------
# (1) flag on: exact append and idempotence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent,subagents,orchestration", _COMBINATIONS)
@pytest.mark.asyncio
async def test_flag_on_appends_exactly_one_serialization_instruction(
    monkeypatch: pytest.MonkeyPatch,
    agent: str | None,
    subagents: list[str] | None,
    orchestration: OrchestrationMode,
) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    baseline_body = json.loads(
        await _capture_payload(agent=agent, subagents=subagents, orchestration=orchestration)
    )
    baseline_instructions = baseline_body.get("instructions")

    monkeypatch.setenv(ENV_FLAG, "1")
    body = json.loads(
        await _capture_payload(agent=agent, subagents=subagents, orchestration=orchestration)
    )
    instructions = body["instructions"]

    if baseline_instructions is None:
        assert instructions == EXPECTED_INSTRUCTION
    else:
        assert instructions == f"{baseline_instructions} {EXPECTED_INSTRUCTION}"

    # Appended exactly once, and nothing else about the payload changed.
    assert instructions.count(EXPECTED_INSTRUCTION) == 1
    body.pop("instructions")
    baseline_body.pop("instructions", None)
    assert body == baseline_body


@pytest.mark.asyncio
async def test_repeated_calls_are_idempotent_under_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    first = await _capture_payload(
        agent="infra", subagents=["security"], orchestration=OrchestrationMode.EXPLICIT
    )
    second = await _capture_payload(
        agent="infra", subagents=["security"], orchestration=OrchestrationMode.EXPLICIT
    )
    assert first == second
    assert json.loads(first)["instructions"].count(EXPECTED_INSTRUCTION) == 1


def test_flag_is_evaluated_at_runtime_not_cached_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert client_base._acceptance_strict_json_enabled() is False
    monkeypatch.setenv(ENV_FLAG, "1")
    assert client_base._acceptance_strict_json_enabled() is True
    monkeypatch.setenv(ENV_FLAG, "0")
    assert client_base._acceptance_strict_json_enabled() is False


def test_instruction_is_serialization_only_and_carries_no_task_intent() -> None:
    text = EXPECTED_INSTRUCTION.lower()
    for forbidden in ("github", "repository", "read", "tool", "agent", "commit"):
        assert forbidden not in text
    assert "json" in text
    assert "key names" in text


# ---------------------------------------------------------------------------
# (3) no new MCP argument / config field; tool contract unchanged
# ---------------------------------------------------------------------------


def test_tool_contract_surface_is_unchanged_at_27() -> None:
    assert expected_tool_count() == 27
    assert len(required_tools()) == 27


def test_flag_is_not_exposed_as_config_field_or_mcp_argument() -> None:
    assert not any("acceptance_strict_json" in name for name in Settings.model_fields)
    server_source = (ROOT / "src" / "hermes_mcp_bridge" / "server.py").read_text(encoding="utf-8")
    config_source = (ROOT / "src" / "hermes_mcp_bridge" / "config.py").read_text(encoding="utf-8")
    assert ENV_FLAG not in server_source
    assert ENV_FLAG not in config_source


# ---------------------------------------------------------------------------
# (4) launcher sets the flag only in the shadow bridge environment
# ---------------------------------------------------------------------------


def test_launcher_sets_flag_only_in_shadow_bridge_env() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    assert text.count(ENV_FLAG) == 1
    assert f"  {ENV_FLAG}='1' \\\n  \"$VENV/bin/hermes-mcp-bridge\"" in text
    # Not exported into the shadow Hermes gateway process.
    gateway_block = text.split('"$HERMES_BIN" gateway run')[0].rsplit("setsid env -i", 1)[-1]
    assert ENV_FLAG not in gateway_block


def test_flag_absent_from_production_deployment_defaults() -> None:
    candidates = [
        ROOT / "compose.yml",
        ROOT / "Dockerfile",
        ROOT / ".env.example",
    ]
    candidates += sorted((ROOT / "deploy").rglob("*.yml"))
    candidates += sorted((ROOT / "deploy").rglob("*.sh"))
    candidates += sorted((ROOT / "docs").rglob("*.md"))
    for path in candidates:
        if not path.is_file():
            continue
        assert ENV_FLAG not in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------------------
# (5) strict acceptance parser
# ---------------------------------------------------------------------------


def test_strict_parser_accepts_exact_object() -> None:
    payload = {"session_id": "s1", "result": {"number": 54, "state": "open"}}
    assert collector._shadow_data_strict(payload) == {"number": 54, "state": "open"}


def test_strict_parser_accepts_whole_string_json_object() -> None:
    payload = {"session_id": "s1", "output": '{"number": 54, "state": "open"}'}
    assert collector._shadow_data_strict(payload) == {"number": 54, "state": "open"}


@pytest.mark.parametrize(
    "answer",
    [
        'Here is the result: {"number": 54}',
        '{"number": 54} trailing prose',
        '```json\n{"number": 54}\n```',
        '{"number": 54}{"number": 55}',
        '{"number": 54}\n{"number": 55}',
        '[{"number": 54}]',
        '"just a string"',
        "54",
        "{}",
        "{not json}",
        "",
    ],
)
def test_strict_parser_rejects_non_exact_answers(answer: str) -> None:
    assert collector._shadow_data_strict({"session_id": "s1", "output": answer}) is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "text",
        [],
        {"session_id": "s1"},
        {"result": {"output": {"number": 54}}},
        {"result": {"number": 54}, "output": {"number": 54}},
        {"result": []},
        {"result": 54},
        {"result": {}},
    ],
)
def test_strict_parser_fails_closed_on_bad_envelopes(payload: Any) -> None:
    assert collector._shadow_data_strict(payload) is None


def test_connected_acceptance_uses_the_strict_parser_only() -> None:
    source = COLLECTOR_PATH.read_text(encoding="utf-8")
    collection = source.split("# ---- V1 agentic shadow ----", 1)[1]
    assert "_shadow_data_strict(shadow_payload)" in collection
    assert "_shadow_data(shadow_payload)" not in collection


def test_strict_parser_does_no_brace_repair_or_retry() -> None:
    source = COLLECTOR_PATH.read_text(encoding="utf-8")
    body = source.split("def _shadow_data_strict(", 1)[1].split("\nasync def ", 1)[0]
    for forbidden in ('find("{")', 'rfind("}")', "while ", "for _ in range"):
        assert forbidden not in body
