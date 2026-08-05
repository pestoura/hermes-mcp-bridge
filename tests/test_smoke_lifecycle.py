from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from hermes_mcp_bridge.contracts import CURRENT_CONTRACT_VERSION

SMOKE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "smoke_test.py"
SPEC = importlib.util.spec_from_file_location("smoke_test_repro", SMOKE_PATH)
smoke_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke_module
SPEC.loader.exec_module(smoke_module)


class _FakeReadStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeWriteStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, read_stream, write_stream) -> None:
        self.read_stream = read_stream
        self.write_stream = write_stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def initialize(self) -> None:
        return None

    async def list_tools(self):
        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name

        names = {
            "hermes_prompt",
            "hermes_submit",
            "hermes_wait",
            "hermes_status",
            "hermes_stop",
            "hermes_health",
            "hermes_readiness",
            "hermes_recent_runs",
            "hermes_capabilities",
            "hermes_agent_card",
            "hermes_policy_evaluate",
            "hermes_approval_create",
            "hermes_approval_respond",
            "hermes_approval_status",
            "hermes_result_manifest",
            "hermes_plan",
            "hermes_execute_approved_plan",
            "hermes_checkpoint_create",
            "hermes_checkpoint_status",
            "hermes_continue",
            "hermes_saga_start",
            "hermes_saga_status",
            "hermes_saga_compensate",
            "hermes_lock_acquire",
            "hermes_lock_status",
            "hermes_lock_release",
            "hermes_quota_status",
        }
        return type("Result", (), {"tools": [_Tool(name) for name in names]})()

    async def call_tool(self, name: str, arguments: dict[str, object], **kwargs: object):
        if name == "hermes_health":
            payload = (
                '{"upstream":{"status":"healthy"},'
                '"bridge":{'
                '"state_registry":{"status":"up"},'
                '"schema_version":"0.6.1",'
                f'"manifest_version":"{CURRENT_CONTRACT_VERSION}",'
                '"manifest_hash":"abc"}'
                '}'
            )
        elif name == "hermes_capabilities":
            payload = (
                f'{{"bridge_version":"{CURRENT_CONTRACT_VERSION}",'
                '"schema_version":"0.6.1",'
                f'"manifest_version":"{CURRENT_CONTRACT_VERSION}",'
                '"manifest_hash":"abc",'
                '"upstream_capabilities_source":"upstream"}'
            )
        elif name == "hermes_agent_card":
            payload = (
                '{"schema_version":"0.6.1",'
                f'"version":"{CURRENT_CONTRACT_VERSION}",'
                '"card_hash":"abc"}'
            )
        else:
            payload = '{"execution_id":"run-1","status":"completed"}'
        return type("Result", (), {"content": [type("Item", (), {"text": payload})()]})()


class _FakeStreamable:
    def __init__(self, *, exit_exc: BaseException | None = None) -> None:
        self.exit_exc = exit_exc

    async def __aenter__(self):
        return (_FakeReadStream(), _FakeWriteStream(), lambda: "session-1")

    async def __aexit__(self, exc_type, exc, tb):
        if self.exit_exc is not None:
            raise self.exit_exc
        return False


@pytest.mark.asyncio
async def test_smoke_exits_cleanly_when_aexit_raises_read_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    streamable = _FakeStreamable(exit_exc=httpx.ReadError("stream closed"))

    def fake_client(*args, **kwargs):
        return streamable

    with (
        patch.object(smoke_module, "streamable_http_client", fake_client),
        patch.object(smoke_module, "ClientSession", _FakeSession),
    ):
        await smoke_module._run("http://127.0.0.1:8766/mcp", "Say hello", 1.0)

    captured = capsys.readouterr().out
    assert "smoke_lifecycle_warning" in captured
    assert "Say hello" not in captured


@pytest.mark.asyncio
async def test_smoke_propagates_errors_before_validation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _BadSession(_FakeSession):
        async def initialize(self) -> None:
            raise RuntimeError("connect failed")

    streamable = _FakeStreamable(exit_exc=httpx.ReadError("stream closed"))

    def fake_client(*args, **kwargs):
        return streamable

    with (
        patch.object(smoke_module, "streamable_http_client", fake_client),
        patch.object(smoke_module, "ClientSession", _BadSession),
        pytest.raises(RuntimeError, match="connect failed"),
    ):
        await smoke_module._run("http://127.0.0.1:8766/mcp", "Say hello", 1.0)


@pytest.mark.asyncio
async def test_smoke_propagates_streamable_aenter_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FailingStreamable:
        async def __aenter__(self):
            raise ConnectionError("connect failed")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_client(*args, **kwargs):
        return _FailingStreamable()

    with (
        patch.object(smoke_module, "streamable_http_client", fake_client),
        patch.object(smoke_module, "ClientSession", _FakeSession),
        pytest.raises(ConnectionError, match="connect failed"),
    ):
        await smoke_module._run("http://127.0.0.1:8766/mcp", "Say hello", 1.0)
