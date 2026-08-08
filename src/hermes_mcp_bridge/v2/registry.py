"""Canonical tool registry and deterministic capability snapshot.

The snapshot is the auditable description of the exact tool surface: it holds
**only non-secret metadata**, is serialized canonically (see
:mod:`hermes_mcp_bridge.v2.canonical`) and is digested with SHA-256 into
``capability_snapshot_hash``.

Determinism contract:

* tools are ordered by ``tool_id``, capabilities by ``capability_id``;
* object keys are sorted, separators fixed, encoding UTF-8;
* no timestamps, paths, hostnames, counters or process-local values;
* insertion order cannot change the bytes;
* any material change (tool added/removed, schema, state, version, tier,
  policy action, ...) changes the digest.
"""

from __future__ import annotations

from typing import Any

from .canonical import canonical_json_bytes, sha256_hex
from .capabilities import CapabilityDescriptor, CapabilityRegistry
from .enums import CapabilityState
from .errors import (
    DuplicateToolError,
    RegistryFrozenError,
    RegistryValidationError,
    UnknownToolError,
)
from .schema import ToolDefinition, normalize_identifier

#: Version of the snapshot envelope itself. Bumping it changes every hash, so
#: it is only bumped when the canonical envelope shape changes.
SNAPSHOT_SCHEMA_VERSION = "v2.phase1.1"


class CapabilitySnapshot:
    """Immutable canonical view over a registry pair."""

    __slots__ = ("_bytes", "_hash", "_payload")

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self._bytes = canonical_json_bytes(payload)
        self._hash = sha256_hex(self._bytes)

    @property
    def payload(self) -> dict[str, Any]:
        return self._payload

    @property
    def canonical_bytes(self) -> bytes:
        return self._bytes

    @property
    def canonical_json(self) -> str:
        return self._bytes.decode("utf-8")

    @property
    def capability_snapshot_hash(self) -> str:
        """Lowercase 64-hex SHA-256 of the canonical byte stream."""
        return self._hash

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CapabilitySnapshot) and other._hash == self._hash

    def __hash__(self) -> int:
        return hash(self._hash)


class ToolRegistry:
    """Deterministic tool registry with controlled mutation and duplicate checks."""

    __slots__ = ("_capabilities", "_frozen", "_tools")

    def __init__(
        self,
        capabilities: CapabilityRegistry | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> None:
        self._capabilities = capabilities if capabilities is not None else CapabilityRegistry()
        self._tools: dict[str, ToolDefinition] = {}
        self._frozen = False
        for tool in tools or []:
            self.register(tool)

    # --- capabilities -----------------------------------------------------
    @property
    def capabilities(self) -> CapabilityRegistry:
        return self._capabilities

    def register_capability(self, capability: CapabilityDescriptor) -> CapabilityDescriptor:
        if self._frozen:
            raise RegistryFrozenError("tool registry is frozen")
        return self._capabilities.register(capability)

    # --- tools ------------------------------------------------------------
    def register(self, tool: ToolDefinition) -> ToolDefinition:
        if self._frozen:
            raise RegistryFrozenError("tool registry is frozen")
        if tool.tool_id in self._tools:
            raise DuplicateToolError(f"duplicate tool_id: {tool.tool_id}")
        if not self._capabilities.contains(tool.capability_id):
            raise RegistryValidationError(
                f"tool {tool.tool_id} references unknown capability {tool.capability_id}"
            )
        if tool.credential_capability_id is not None and not self._capabilities.contains(
            tool.credential_capability_id
        ):
            raise RegistryValidationError(
                f"tool {tool.tool_id} references unknown credential capability "
                f"{tool.credential_capability_id}"
            )
        self._tools[tool.tool_id] = tool
        return tool

    def freeze(self) -> ToolRegistry:
        self._capabilities.freeze()
        self._frozen = True
        return self

    @property
    def frozen(self) -> bool:
        return self._frozen

    # --- lookup (fail-closed) --------------------------------------------
    def get(self, tool_id: str) -> ToolDefinition:
        try:
            key = normalize_identifier(tool_id, field="tool_id")
        except Exception as exc:
            raise UnknownToolError(f"unknown tool: {tool_id!r}") from exc
        try:
            return self._tools[key]
        except KeyError as exc:
            raise UnknownToolError(f"unknown tool: {key}") from exc

    def contains(self, tool_id: str) -> bool:
        try:
            self.get(tool_id)
        except UnknownToolError:
            return False
        return True

    def __contains__(self, tool_id: object) -> bool:
        return isinstance(tool_id, str) and self.contains(tool_id)

    def __len__(self) -> int:
        return len(self._tools)

    def ordered(self) -> list[ToolDefinition]:
        """Tools sorted by ``tool_id`` — independent of insertion order."""
        return [self._tools[key] for key in sorted(self._tools)]

    def capability_state(self, tool: ToolDefinition) -> CapabilityState:
        return self._capabilities.get(tool.capability_id).state

    # --- snapshot ---------------------------------------------------------
    def snapshot_payload(self) -> dict[str, Any]:
        return {
            "capabilities": self._capabilities.canonical(),
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "tools": [tool.canonical() for tool in self.ordered()],
        }

    def snapshot(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(self.snapshot_payload())

    def capability_snapshot_hash(self) -> str:
        return self.snapshot().capability_snapshot_hash


__all__ = ["SNAPSHOT_SCHEMA_VERSION", "CapabilitySnapshot", "ToolRegistry"]
