import asyncio
import inspect
from typing import Any

import pytest

from hermes_mcp_bridge.factory_northbound import (
    FactoryNorthboundUnavailable,
    register_factory_northbound_tools,
)


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):  # type: ignore[no-untyped-def]
        def decorate(function):  # type: ignore[no-untyped-def]
            self.tools[function.__name__] = function
            return function

        return decorate


class FakeFactoryPort:
    def status(self, *, candidate_sha: str, principal: str) -> dict[str, object]:
        return {"operation": "STATUS", "candidate_sha": candidate_sha, "principal": principal}

    def evidence(self, *, candidate_sha: str, principal: str) -> dict[str, object]:
        return {"operation": "EVIDENCE", "candidate_sha": candidate_sha, "principal": principal}

    def acceptance(self, *, candidate_sha: str, principal: str) -> dict[str, object]:
        return {"operation": "ACCEPTANCE", "candidate_sha": candidate_sha, "principal": principal}

    def protected_mutation_intent(
        self,
        *,
        candidate_sha: str,
        principal: str,
        action: str,
        resource: str,
        authority_evidence_id: str,
        human_decision_id: str,
    ) -> dict[str, object]:
        return {
            "operation": "PROTECTED_MUTATION_INTENT",
            "candidate_sha": candidate_sha,
            "principal": principal,
            "action": action,
            "resource": resource,
            "authority_evidence_id": authority_evidence_id,
            "human_decision_id": human_decision_id,
            "execute": False,
        }


def test_factory_runtime_is_absent_by_default() -> None:
    mcp = FakeMCP()
    registered = register_factory_northbound_tools(mcp, enabled=False, port=None)

    assert registered == ()
    assert mcp.tools == {}


def test_explicit_enable_without_factory_port_fails_closed() -> None:
    with pytest.raises(FactoryNorthboundUnavailable, match="control port"):
        register_factory_northbound_tools(FakeMCP(), enabled=True, port=None)


def test_factory_runtime_registers_only_closed_external_surface() -> None:
    mcp = FakeMCP()
    registered = register_factory_northbound_tools(mcp, enabled=True, port=FakeFactoryPort())

    assert registered == (
        "factory_acceptance",
        "factory_evidence",
        "factory_protected_mutation_intent",
        "factory_status",
    )
    assert tuple(sorted(mcp.tools)) == registered
    assert all("origin" not in inspect.signature(tool).parameters for tool in mcp.tools.values())
    assert "hermes_prompt" not in mcp.tools


def test_factory_runtime_delegates_read_and_never_executes_mutation() -> None:
    mcp = FakeMCP()
    register_factory_northbound_tools(mcp, enabled=True, port=FakeFactoryPort())

    status = asyncio.run(mcp.tools["factory_status"]("sha-1", "operator"))
    assert status["operation"] == "STATUS"

    intent = asyncio.run(
        mcp.tools["factory_protected_mutation_intent"](
            "sha-1",
            "operator",
            "RELEASE",
            "release:factory",
            "evidence:authority",
            "decision:owner",
        )
    )
    assert intent["operation"] == "PROTECTED_MUTATION_INTENT"
    assert intent["execute"] is False
