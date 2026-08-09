from __future__ import annotations

import importlib
from types import ModuleType

import pytest


def server_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("HERMES_API_KEY", "unit-test-key-0123456789")
    return importlib.import_module("hermes_mcp_bridge.server")


def test_streamable_http_uses_sse_response_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = server_module(monkeypatch)

    assert server.mcp.settings.json_response is False
    assert server.mcp.settings.stateless_http is True


def test_progress_messages_hide_reasoning_and_message_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = server_module(monkeypatch)

    assert (
        server._progress_message({"event": "reasoning.available", "text": "private reasoning"})
        is None
    )
    assert server._progress_message({"event": "message.delta", "delta": "partial answer"}) is None


def test_progress_messages_cover_long_run_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = server_module(monkeypatch)

    accepted = server._progress_message(
        {
            "event": "bridge.run.accepted",
            "run_id": "run-1",
            "session_id": "session-1",
        }
    )
    heartbeat = server._progress_message(
        {
            "event": "bridge.heartbeat",
            "status": "running",
            "elapsed_seconds": 900,
        }
    )
    completed = server._progress_message({"event": "run.completed"})

    assert accepted == "Hermes accepted run run-1 in session session-1."
    assert heartbeat == "Hermes is still working (running, 900s elapsed)."
    assert completed == "Hermes completed the run and is returning the final result."
