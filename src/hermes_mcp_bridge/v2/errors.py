"""Error taxonomy for the V2 registry core.

Every error message is safe to log: callers must never place credential
material, secret paths or raw arguments in these messages.
"""

from __future__ import annotations


class V2Error(Exception):
    """Base class for all V2 registry-core errors."""


class RegistryValidationError(V2Error, ValueError):
    """A tool/capability definition violated a canonical invariant."""


class DuplicateToolError(RegistryValidationError):
    """A tool_id was registered more than once."""


class DuplicateCapabilityError(RegistryValidationError):
    """A capability_id was registered more than once."""


class UnknownToolError(V2Error, KeyError):
    """Fail-closed lookup of a tool that is not registered."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.args[0]) if self.args else self.__class__.__name__


class UnknownCapabilityError(V2Error, KeyError):
    """Fail-closed lookup of a capability that is not registered."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.args[0]) if self.args else self.__class__.__name__


class RegistryFrozenError(V2Error, RuntimeError):
    """Mutation attempted on a frozen registry."""


class PolicyValidationError(V2Error, ValueError):
    """A policy rule set is not acceptable (e.g. permissive wildcard)."""


__all__ = [
    "DuplicateCapabilityError",
    "DuplicateToolError",
    "PolicyValidationError",
    "RegistryFrozenError",
    "RegistryValidationError",
    "UnknownCapabilityError",
    "UnknownToolError",
    "V2Error",
]
