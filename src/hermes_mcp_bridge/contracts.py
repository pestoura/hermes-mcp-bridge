"""Versioned tool contract for the Hermes MCP Bridge.

This module is the single source of truth for *which* tools each contract
version advertises. It exists to remove blind global constants (such as a
hard-coded ``26``) from scripts, docs and tests: callers validate against an
explicit required set for a given version, and the count is always derived
from that set.

No secrets, paths or runtime values are stored here.
"""

from __future__ import annotations

from types import MappingProxyType

#: Tools that exist since the 0.6.x contract line.
_TOOLS_0_6: frozenset[str] = frozenset(
    {
        "hermes_submit",
        "hermes_prompt",
        "hermes_wait",
        "hermes_status",
        "hermes_stop",
        "hermes_health",
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
)

#: Tools added by the 0.8.x observability slice.
_TOOLS_ADDED_0_8: frozenset[str] = frozenset({"hermes_readiness"})

#: Required tool set per contract line. Keys are contract versions.
TOOL_CONTRACTS: MappingProxyType[str, frozenset[str]] = MappingProxyType(
    {
        "0.6.0": _TOOLS_0_6,
        "0.6.1": _TOOLS_0_6,
        "0.8.0": _TOOLS_0_6 | _TOOLS_ADDED_0_8,
        "0.8.1": _TOOLS_0_6 | _TOOLS_ADDED_0_8,
        "0.8.2": _TOOLS_0_6 | _TOOLS_ADDED_0_8,
    }
)

#: Contract version implemented by this build.
CURRENT_CONTRACT_VERSION = "0.8.2"

#: Wire schema version. Intentionally unchanged in 0.8.x.
SCHEMA_VERSION = "0.6.1"


class UnknownContractVersionError(ValueError):
    """Raised when a contract version has no declared tool set."""


def required_tools(version: str = CURRENT_CONTRACT_VERSION) -> frozenset[str]:
    """Return the mandatory tool set for ``version``."""

    try:
        return TOOL_CONTRACTS[version]
    except KeyError as exc:  # pragma: no cover - defensive
        raise UnknownContractVersionError(version) from exc


def expected_tool_count(version: str = CURRENT_CONTRACT_VERSION) -> int:
    """Return the expected tool count derived from the required set."""

    return len(required_tools(version))


def diff_tools(
    observed: object, version: str = CURRENT_CONTRACT_VERSION
) -> dict[str, list[str]]:
    """Compare an observed tool collection against the contract.

    Returns a mapping with sorted ``missing`` and ``extra`` lists. ``extra`` is
    informational: additive tools are allowed by policy, ``missing`` is not.
    """

    names = {str(name) for name in observed}  # type: ignore[union-attr]
    required = required_tools(version)
    return {
        "missing": sorted(required - names),
        "extra": sorted(names - required),
    }


def validate_tools(
    observed: object, version: str = CURRENT_CONTRACT_VERSION
) -> dict[str, object]:
    """Validate an observed tool collection against the contract."""

    diff = diff_tools(observed, version=version)
    count = len({str(name) for name in observed})  # type: ignore[union-attr]
    return {
        "version": version,
        "ok": not diff["missing"],
        "count": count,
        "expected_count": expected_tool_count(version),
        "missing": diff["missing"],
        "extra": diff["extra"],
    }
