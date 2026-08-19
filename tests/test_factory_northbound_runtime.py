import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_mcp_bridge.factory_northbound import (
    FactoryLibraryControlPort,
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


class FakeCaller:
    def __init__(self, *, principal: str, origin: object) -> None:
        self.principal = principal
        self.origin = origin


class FakeAction:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeControl:
    def __init__(self, registry: object) -> None:
        self.registry = registry

    def status(self, *, candidate_sha: str, caller: FakeCaller) -> dict[str, object]:
        return {"operation": "STATUS", "candidate_sha": candidate_sha, "caller": caller}

    def evidence(self, *, candidate_sha: str, caller: FakeCaller) -> dict[str, object]:
        return {"operation": "EVIDENCE", "candidate_sha": candidate_sha, "caller": caller}

    def acceptance(self, *, candidate_sha: str, caller: FakeCaller) -> dict[str, object]:
        return {"operation": "ACCEPTANCE", "candidate_sha": candidate_sha, "caller": caller}

    def protected_mutation_intent(self, **kwargs: object) -> dict[str, object]:
        return {"operation": "PROTECTED_MUTATION_INTENT", "execute": False, **kwargs}


def _factory_module_loader(name: str) -> object:
    if name == "hermes_factory.traceability.registry":
        return SimpleNamespace(SemanticRegistry=lambda path: {"registry_path": str(path)})
    if name == "hermes_factory.control.northbound":
        return SimpleNamespace(
            NorthboundControl=FakeControl,
            NorthboundCaller=FakeCaller,
            NorthboundOrigin=SimpleNamespace(EXTERNAL="EXTERNAL"),
            ProtectedMutationAction=FakeAction,
        )
    raise ModuleNotFoundError(name)


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


def test_library_port_lazy_binds_external_origin_and_current_factory_contract() -> None:
    port = FactoryLibraryControlPort(
        "/var/lib/hermes-factory/factory.sqlite3",
        module_loader=_factory_module_loader,
    )

    status = port.status(candidate_sha="sha-1", principal="operator")
    caller = status["caller"]
    assert isinstance(caller, FakeCaller)
    assert caller.principal == "operator"
    assert caller.origin == "EXTERNAL"

    intent = port.protected_mutation_intent(
        candidate_sha="sha-1",
        principal="operator",
        action="RELEASE",
        resource="release:factory",
        authority_evidence_id="evidence:authority",
        human_decision_id="decision:owner",
    )
    assert intent["execute"] is False
    assert isinstance(intent["action"], FakeAction)
    assert intent["action"].value == "RELEASE"


def test_library_port_fails_closed_when_factory_package_or_registry_path_is_missing() -> None:
    with pytest.raises(FactoryNorthboundUnavailable, match="registry path"):
        FactoryLibraryControlPort("   ", module_loader=_factory_module_loader)

    def missing_factory(_: str) -> object:
        raise ModuleNotFoundError("hermes_factory")

    with pytest.raises(FactoryNorthboundUnavailable, match="hermes-factory"):
        FactoryLibraryControlPort("/factory.db", module_loader=missing_factory)
