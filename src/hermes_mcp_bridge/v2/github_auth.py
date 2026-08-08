"""GitHub DIRECT authorization material contract for V2 Phase 2.

Phase 1 deliberately accepted only a credential *status* broker. DIRECT backend
calls additionally need short-lived authorization material at the final
execution boundary. This module defines that narrow boundary without choosing a
real Jarvas secret backend.

Security properties:

* callers request the stable capability id ``github.read`` plus an exact
  repository resource;
* raw authorization material is private, never canonicalized, never exposed as
  a property and always redacted from ``str``/``repr``;
* CR/LF and empty values are rejected before an HTTP header can be created;
* the only implementation shipped here is an in-memory static provider for
  hermetic tests. A real provider remains a Phase 2 connected-runtime gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


class GitHubAuthorizationError(ValueError):
    """Invalid or unavailable GitHub authorization material."""


class GitHubAuthorization:
    """Opaque bearer material with a deliberately redacted public surface."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise GitHubAuthorizationError("authorization material must be a string")
        if not value or not value.strip():
            raise GitHubAuthorizationError("authorization material must not be empty")
        if "\r" in value or "\n" in value:
            raise GitHubAuthorizationError("authorization material contains invalid characters")
        self.__value = value

    def header_value(self) -> str:
        """Return the Authorization header value at the last possible boundary."""
        return f"Bearer {self.__value}"

    def __repr__(self) -> str:
        return "GitHubAuthorization(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


@runtime_checkable
class GitHubAuthorizationProvider(Protocol):
    """Resolve authorization material for one capability and repository."""

    def resolve(
        self,
        capability_id: str,
        repository: str,
    ) -> GitHubAuthorization | None: ...


class StaticGitHubAuthorizationProvider:
    """In-memory provider for tests only; never a production secret backend."""

    __slots__ = ("_entries", "resolve_calls")

    def __init__(
        self,
        entries: Mapping[tuple[str, str], GitHubAuthorization] | None = None,
    ) -> None:
        self._entries = {
            (capability.strip().lower(), repository.strip().lower()): material
            for (capability, repository), material in (entries or {}).items()
        }
        self.resolve_calls = 0

    def resolve(
        self,
        capability_id: str,
        repository: str,
    ) -> GitHubAuthorization | None:
        self.resolve_calls += 1
        return self._entries.get(
            (capability_id.strip().lower(), repository.strip().lower())
        )

    def __repr__(self) -> str:
        count = len(self._entries)
        return f"StaticGitHubAuthorizationProvider(entries={count}, material=<redacted>)"


__all__ = [
    "GitHubAuthorization",
    "GitHubAuthorizationError",
    "GitHubAuthorizationProvider",
    "StaticGitHubAuthorizationProvider",
]
