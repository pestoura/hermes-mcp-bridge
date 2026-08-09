"""Least-privilege GitHub **write** credentials and capabilities (Phase 3, L1).

Implements ``docs/v2/phase3/credential-split.md`` and ADR-0020. It is the only
module that knows how Phase 3 write material is located, classified and
authorized, and it is deliberately separate from:

* :mod:`hermes_mcp_bridge.v2.github_secret_provider` — the accepted Phase 2
  ``github.read`` provider, which is left byte-for-byte unchanged here;
* :mod:`hermes_mcp_bridge.v2.github_readiness` — which may only ever expose
  non-secret status.

Security contract enforced here (every clause has a test):

* **Disjointness.** ``github.read`` and ``github.write.*`` are different
  capability universes. A write provider refuses the read capability id, a read
  capability id can never be resolved to write material, and no write
  capability id may equal :data:`~hermes_mcp_bridge.v2.enums.READ_CAPABILITY_ID`.
  A read-only tool is never satisfied by a write capability, and vice versa.
* **Exact permissions.** The granted permission map must equal the intended map
  for that capability. A *superset* is a failure (``PERMISSION_SUPERSET``), not
  a convenience, and the ``Administration`` permission is rejected outright
  (``ADMINISTRATION_PERMISSION_PRESENT``) so repository deletion is
  unrepresentable rather than merely denied.
* **No credential exposure.** Nothing in this module returns, logs, formats or
  canonicalizes material, a secret path or an environment variable name.
  ``repr``/``str`` are redacted; every denial is a stable, path-free
  :class:`~hermes_mcp_bridge.v2.enums.MutationReasonCode`.
* **Fail closed.** Any unknown, unreadable, mismatched, over-permissioned or
  unattested state yields a non-ready
  :class:`~hermes_mcp_bridge.v2.enums.CapabilityState` and ``resolve`` returns
  ``None``. There is no permissive default and no cached readiness.
* **Runtime gate.** :meth:`WriteCapabilityBroker.authorize` grants a write only
  when the capability is *configured, authenticated, healthy and
  policy-allowed* — four independent conditions, all required.
* **No classic/broad PAT.** The Phase 2 rejection (prefixed and legacy 40-hex
  forms) is inherited unchanged; material is otherwise opaque and classified by
  prefix only.

Broker abstraction (forward compatibility)
------------------------------------------

:class:`WriteMaterialProvider` is a narrow protocol — ``probe`` plus
``resolve`` — so a future Vault / cloud secret-manager backend is a new class
implementing it, with no change to the broker, the readiness mapping or the
executor. :class:`FileWriteMaterialProvider` is the file-mounted implementation
available today. No Vault dependency is introduced and none is required: the
whole module is exercisable hermetically, including with no write credential
configured at all, in which case readiness reports ``UNAVAILABLE`` with
``WRITE_CAPABILITY_NOT_READY``.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from .credentials import CredentialCapabilityStatus
from .enums import (
    READ_CAPABILITY_ID,
    CapabilityState,
    MutationReasonCode,
    MutationStage,
    WriteCapabilityId,
)
from .errors import WriteCapabilityError
from .github_auth import GitHubAuthorization
from .github_direct import GitHubRepositoryScope
from .github_readiness import exact_permission_failure
from .github_secret_provider import (
    AuthorizationStatus,
    GitHubProviderType,
    MaterialClass,
    classify_material,
)

#: Exact intended permission map per write capability (``credential-split.md``).
#: The granted map must equal this exactly; a superset is a failure.
INTENDED_WRITE_PERMISSIONS: Mapping[WriteCapabilityId, Mapping[str, str]] = MappingProxyType(
    {
        WriteCapabilityId.BRANCH: MappingProxyType(
            {
                "contents": "write",
                "metadata": "read",
            }
        ),
        WriteCapabilityId.PR: MappingProxyType(
            {
                "contents": "read",
                "metadata": "read",
                "pull_requests": "write",
            }
        ),
        WriteCapabilityId.MERGE: MappingProxyType(
            {
                "checks": "read",
                "contents": "write",
                "metadata": "read",
                "pull_requests": "write",
            }
        ),
    }
)

#: Logical secret name per write capability. The provider reads ``<NAME>_FILE``
#: only; a bare ``<NAME>`` environment value carries no permission guarantee and
#: is rejected. Each capability has its own name: one write secret is never
#: shared by two capabilities, and none of them is the read secret.
WRITE_SECRET_NAMES: Mapping[WriteCapabilityId, str] = MappingProxyType(
    {
        WriteCapabilityId.BRANCH: "BRIDGE_V2_GITHUB_WRITE_BRANCH_TOKEN",
        WriteCapabilityId.PR: "BRIDGE_V2_GITHUB_WRITE_PR_TOKEN",
        WriteCapabilityId.MERGE: "BRIDGE_V2_GITHUB_WRITE_MERGE_TOKEN",
    }
)

#: Provider statuses meaning "configured, but authorization was refused".
_DENIED_STATUSES = frozenset(
    {
        AuthorizationStatus.CLASSIC_PAT_REJECTED,
        AuthorizationStatus.ENV_MATERIAL_REJECTED,
        AuthorizationStatus.PROVIDER_TYPE_MISMATCH,
        AuthorizationStatus.CAPABILITY_MISMATCH,
        AuthorizationStatus.REPOSITORY_OUT_OF_SCOPE,
        AuthorizationStatus.FILE_PERMISSIONS_TOO_OPEN,
    }
)

_MAX_MATERIAL_BYTES = 8192
_MIN_MATERIAL_LENGTH = 20
_SYMLINK_ERRNOS = frozenset(
    value for value in (getattr(errno, name, None) for name in ("ELOOP", "EMLINK")) if value
)


def write_capability_ids() -> tuple[str, ...]:
    """Return the write capability ids, sorted. Never includes the read id."""
    return tuple(sorted(member.value for member in WriteCapabilityId))


def is_write_capability(capability_id: str) -> bool:
    """True only for an exact ``github.write.*`` capability id."""
    return _normalize(capability_id) in set(write_capability_ids())


def parse_write_capability(capability_id: str) -> WriteCapabilityId | None:
    """Return the enum member for a write capability id, else ``None``.

    The read capability id never parses to a write capability.
    """
    key = _normalize(capability_id)
    if not key or key == READ_CAPABILITY_ID:
        return None
    for member in WriteCapabilityId:
        if member.value == key:
            return member
    return None


def intended_permissions(capability: WriteCapabilityId) -> Mapping[str, str]:
    """Exact intended permission map for one write capability."""
    return INTENDED_WRITE_PERMISSIONS[capability]


def assert_read_write_disjoint() -> None:
    """Raise when the read/write capability universes ever intersect."""
    write_ids = set(write_capability_ids())
    if READ_CAPABILITY_ID in write_ids:
        raise WriteCapabilityError(
            MutationReasonCode.WRITE_CAPABILITY_MISMATCH,
            MutationStage.CREDENTIAL,
        )
    for value in write_ids:
        if not value.startswith(f"{READ_CAPABILITY_ID.split('.', 1)[0]}.write."):
            raise WriteCapabilityError(
                MutationReasonCode.WRITE_CAPABILITY_MISMATCH,
                MutationStage.CREDENTIAL,
            )


def permission_failure(
    capability: WriteCapabilityId,
    granted: Mapping[str, str] | None,
) -> MutationReasonCode | None:
    """Compare a granted permission map against the intended one.

    ``Administration`` present wins over every other verdict; a superset,
    a missing permission or a weaker/stronger level is a mismatch. ``None``
    (nothing probed) is *not* treated as compliant.
    """
    return exact_permission_failure(intended_permissions(capability), granted)


@dataclass(frozen=True, slots=True)
class WriteCapabilityReadiness:
    """Non-secret readiness view of exactly one write capability.

    Carries no value, no path and no environment variable name; ``reason`` is a
    stable redacted code and is ``None`` only when ``state`` is ``READY``.
    """

    capability_id: str
    provider: str
    state: CapabilityState
    reason: MutationReasonCode | None
    permissions_attested: bool

    @property
    def is_ready(self) -> bool:
        return self.state.is_ready and self.reason is None

    def status(self) -> CredentialCapabilityStatus:
        """Project onto the shared broker status model (still non-secret)."""
        return CredentialCapabilityStatus(
            capability_id=self.capability_id,
            provider=self.provider,
            state=self.state,
        )

    def sanitized(self) -> dict[str, object]:
        """Evidence-safe mapping. Contains no secret-derived field."""
        return {
            "capability_id": self.capability_id,
            "permissions_attested": self.permissions_attested,
            "provider": self.provider,
            "reason": self.reason.value if self.reason else None,
            "ready": self.is_ready,
            "state": self.state.value,
        }

    def __repr__(self) -> str:
        return (
            "WriteCapabilityReadiness("
            f"capability_id={self.capability_id!r}, state={self.state.value!r}, "
            f"reason={self.reason.value if self.reason else None!r})"
        )

    __str__ = __repr__


@runtime_checkable
class WriteMaterialProvider(Protocol):
    """Backend able to probe and resolve write material for one capability.

    Implemented today by :class:`FileWriteMaterialProvider`. A future broker
    backend (Vault, cloud secret manager) implements this protocol and nothing
    else changes.
    """

    @property
    def capability(self) -> WriteCapabilityId:
        """The single write capability this provider serves."""
        ...

    def probe(self) -> AuthorizationStatus:
        """Validate configuration/material without returning any of it."""
        ...

    def resolve(self, capability_id: str, repository: str) -> GitHubAuthorization | None:
        """Return bearer material, or ``None`` on any failure (fail closed)."""
        ...


class FileWriteMaterialProvider:
    """File-mounted write material for exactly one ``github.write.*`` capability.

    The instance holds no secret state: only the environment mapping, the
    capability, the declared provider type and the repository allow-list. The
    file is read on demand at the final boundary and never cached, so rotation
    on disk takes effect without a restart.
    """

    __slots__ = (
        "_capability",
        "_env",
        "_provider_type",
        "_require_secure_mode",
        "_scope",
        "_secret_name",
        "_status",
        "resolve_calls",
    )

    def __init__(
        self,
        *,
        capability: WriteCapabilityId,
        scope: GitHubRepositoryScope,
        provider_type: GitHubProviderType = GitHubProviderType.GITHUB_APP,
        secret_name: str | None = None,
        require_secure_mode: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(capability, WriteCapabilityId):
            raise ValueError("capability must be a WriteCapabilityId")
        if not isinstance(provider_type, GitHubProviderType):
            raise ValueError("provider_type must be a GitHubProviderType")
        name = secret_name if secret_name is not None else WRITE_SECRET_NAMES[capability]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("secret_name must be a non-empty string")
        self._capability = capability
        self._scope = scope
        self._provider_type = provider_type
        self._secret_name = name.strip()
        self._require_secure_mode = bool(require_secure_mode)
        self._env = env
        self._status = AuthorizationStatus.NOT_CONFIGURED
        self.resolve_calls = 0

    # ---- non-secret surface -------------------------------------------------

    @property
    def capability(self) -> WriteCapabilityId:
        return self._capability

    @property
    def provider_type(self) -> GitHubProviderType:
        return self._provider_type

    @property
    def repository_scopes(self) -> tuple[str, ...]:
        return self._scope.repositories

    @property
    def last_status(self) -> AuthorizationStatus:
        return self._status

    def describe(self) -> dict[str, object]:
        """Non-secret description: no value, no path, no env var name."""
        status = self.probe()
        return {
            "capability_id": self._capability.value,
            "configured": status is not AuthorizationStatus.NOT_CONFIGURED,
            "provider_type": self._provider_type.value,
            "repository_scopes": list(self.repository_scopes),
            "secret_source_type": "file",
            "secure_mode_required": self._require_secure_mode,
            "status": status.value,
        }

    def __repr__(self) -> str:
        return (
            "FileWriteMaterialProvider("
            f"capability={self._capability.value!r}, "
            f"repositories={len(self.repository_scopes)}, material=<redacted>)"
        )

    __str__ = __repr__

    # ---- boundary -----------------------------------------------------------

    def probe(self) -> AuthorizationStatus:
        material, status = self._read_material()
        del material
        self._status = status
        return status

    def resolve(self, capability_id: str, repository: str) -> GitHubAuthorization | None:
        self.resolve_calls += 1

        key = _normalize(capability_id)
        if key == READ_CAPABILITY_ID or key != self._capability.value:
            # A read capability id must never reach write material, and one
            # write provider never serves another write capability.
            self._status = AuthorizationStatus.CAPABILITY_MISMATCH
            return None

        if not self._repository_allowed(repository):
            self._status = AuthorizationStatus.REPOSITORY_OUT_OF_SCOPE
            return None

        material, status = self._read_material()
        self._status = status
        if status is not AuthorizationStatus.READY or material is None:
            return None
        try:
            return GitHubAuthorization(material)
        except Exception:
            self._status = AuthorizationStatus.MATERIAL_MALFORMED
            return None
        finally:
            del material

    # ---- internals ----------------------------------------------------------

    def _repository_allowed(self, repository: str) -> bool:
        if not isinstance(repository, str) or repository.count("/") != 1:
            return False
        owner, repo = repository.split("/", 1)
        try:
            return self._scope.allows(owner, repo)
        except Exception:
            return False

    def _environ(self) -> Mapping[str, str]:
        return self._env if self._env is not None else os.environ

    def _read_fd_validated(self, target: str) -> tuple[bytes | None, AuthorizationStatus]:
        """Open, validate and read through a single descriptor (no TOCTOU window)."""
        flags = os.O_RDONLY
        for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"):
            flags |= getattr(os, name, 0)
        try:
            fd = os.open(target, flags)
        except OSError as exc:
            if getattr(exc, "errno", None) in _SYMLINK_ERRNOS:
                return None, AuthorizationStatus.FILE_NOT_REGULAR
            return None, AuthorizationStatus.FILE_UNREADABLE
        try:
            try:
                info = os.fstat(fd)
            except OSError:
                return None, AuthorizationStatus.FILE_UNREADABLE
            if not stat.S_ISREG(info.st_mode):
                return None, AuthorizationStatus.FILE_NOT_REGULAR
            if self._require_secure_mode and (stat.S_IMODE(info.st_mode) & 0o077):
                return None, AuthorizationStatus.FILE_PERMISSIONS_TOO_OPEN
            try:
                raw = _read_all(fd, _MAX_MATERIAL_BYTES + 1)
            except OSError:
                return None, AuthorizationStatus.FILE_UNREADABLE
        finally:
            with contextlib.suppress(OSError):  # pragma: no cover - defensive
                os.close(fd)
        return raw, AuthorizationStatus.READY

    def _read_material(self) -> tuple[str | None, AuthorizationStatus]:
        environ = self._environ()
        path = environ.get(f"{self._secret_name}_FILE")
        if not path or not str(path).strip():
            if environ.get(self._secret_name):
                return None, AuthorizationStatus.ENV_MATERIAL_REJECTED
            return None, AuthorizationStatus.NOT_CONFIGURED

        raw, status = self._read_fd_validated(str(path).strip())
        if raw is None:
            return None, status
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None, AuthorizationStatus.MATERIAL_MALFORMED

        if not value:
            return None, AuthorizationStatus.FILE_EMPTY
        # Resource bound only: material is opaque and classified by prefix.
        too_long = len(value.encode("utf-8")) > _MAX_MATERIAL_BYTES
        if too_long or len(value) < _MIN_MATERIAL_LENGTH or any(c.isspace() for c in value):
            return None, AuthorizationStatus.MATERIAL_MALFORMED

        observed = classify_material(value)
        if observed is MaterialClass.CLASSIC_PAT:
            return None, AuthorizationStatus.CLASSIC_PAT_REJECTED
        expected = (
            MaterialClass.GITHUB_APP
            if self._provider_type is GitHubProviderType.GITHUB_APP
            else MaterialClass.FINE_GRAINED_TOKEN
        )
        if observed is not expected:
            return None, AuthorizationStatus.PROVIDER_TYPE_MISMATCH
        return value, AuthorizationStatus.READY


class WriteCapabilityBroker:
    """Fail-closed readiness and authorization view over write providers.

    The broker returns *status only*. The single method able to hand out
    material, :meth:`authorize`, requires all four runtime conditions of
    ADR-0020 to hold — configured, authenticated, healthy and policy-allowed —
    and refuses the read capability id unconditionally.
    """

    __slots__ = ("_permissions", "_policy_allows", "_provider_name", "_providers")

    def __init__(
        self,
        providers: Iterable[WriteMaterialProvider] = (),
        *,
        attested_permissions: Mapping[WriteCapabilityId, Mapping[str, str]] | None = None,
        policy_allows: Mapping[WriteCapabilityId, bool] | None = None,
        provider_name: str = "github",
    ) -> None:
        self._providers: dict[WriteCapabilityId, WriteMaterialProvider] = {}
        for provider in providers:
            capability = provider.capability
            if not isinstance(capability, WriteCapabilityId):
                raise ValueError("provider.capability must be a WriteCapabilityId")
            if capability in self._providers:
                raise ValueError("one provider per write capability")
            self._providers[capability] = provider
        self._permissions = dict(attested_permissions or {})
        self._policy_allows = dict(policy_allows or {})
        self._provider_name = provider_name

    # ---- status -------------------------------------------------------------

    def readiness(self, credential_capability_id: str) -> WriteCapabilityReadiness | None:
        """Return the non-secret readiness, or ``None`` for a non-write id.

        ``github.read`` yields ``None``: this broker cannot speak about the read
        capability at all, so a healthy read path can never be mistaken for a
        ready write path.
        """
        capability = parse_write_capability(credential_capability_id)
        if capability is None:
            return None

        provider = self._providers.get(capability)
        if provider is None:
            return self._not_ready(
                capability,
                CapabilityState.UNAVAILABLE,
                MutationReasonCode.WRITE_CAPABILITY_NOT_READY,
            )

        try:
            probe_status = provider.probe()
        except Exception:
            probe_status = AuthorizationStatus.FILE_UNREADABLE

        if probe_status is not AuthorizationStatus.READY:
            state = (
                CapabilityState.DENIED
                if probe_status in _DENIED_STATUSES
                else CapabilityState.UNAVAILABLE
            )
            return self._not_ready(
                capability, state, MutationReasonCode.WRITE_CAPABILITY_NOT_READY
            )

        granted = self._permissions.get(capability)
        failure = permission_failure(capability, granted)
        if failure is not None:
            attested = granted is not None
            return WriteCapabilityReadiness(
                capability_id=capability.value,
                provider=self._provider_name,
                state=CapabilityState.DENIED if attested else CapabilityState.UNAVAILABLE,
                reason=failure,
                permissions_attested=attested,
            )

        if not self._policy_allows.get(capability, False):
            return WriteCapabilityReadiness(
                capability_id=capability.value,
                provider=self._provider_name,
                state=CapabilityState.HEALTHY,
                reason=MutationReasonCode.WRITE_CAPABILITY_NOT_READY,
                permissions_attested=True,
            )

        return WriteCapabilityReadiness(
            capability_id=capability.value,
            provider=self._provider_name,
            state=CapabilityState.READY,
            reason=None,
            permissions_attested=True,
        )

    def status(self, credential_capability_id: str) -> CredentialCapabilityStatus | None:
        readiness = self.readiness(credential_capability_id)
        return readiness.status() if readiness else None

    def is_ready(self, credential_capability_id: str) -> bool:
        readiness = self.readiness(credential_capability_id)
        return bool(readiness and readiness.is_ready)

    def report(self) -> list[dict[str, object]]:
        """Sanitized readiness for every write capability, sorted by id."""
        rows: list[dict[str, object]] = []
        for value in write_capability_ids():
            readiness = self.readiness(value)
            if readiness is not None:
                rows.append(readiness.sanitized())
        return rows

    # ---- authorization ------------------------------------------------------

    def authorize(
        self,
        credential_capability_id: str,
        repository: str,
    ) -> GitHubAuthorization:
        """Resolve write material, or raise a redacted :class:`WriteCapabilityError`.

        Order matters and is fail-closed at every step: a non-write id, a
        not-ready capability or a refusing provider all deny before any material
        is read, and no branch here returns a status describing the secret.
        """
        capability = parse_write_capability(credential_capability_id)
        if capability is None:
            raise WriteCapabilityError(
                MutationReasonCode.READ_CAPABILITY_CANNOT_MUTATE
                if _normalize(credential_capability_id) == READ_CAPABILITY_ID
                else MutationReasonCode.WRITE_CAPABILITY_MISMATCH,
                MutationStage.CREDENTIAL,
            )

        readiness = self.readiness(capability.value)
        if readiness is None or not readiness.is_ready:
            raise WriteCapabilityError(
                (readiness.reason if readiness and readiness.reason else None)
                or MutationReasonCode.WRITE_CAPABILITY_NOT_READY,
                MutationStage.CREDENTIAL,
            )

        provider = self._providers[capability]
        material = provider.resolve(capability.value, repository)
        if material is None:
            raise WriteCapabilityError(
                MutationReasonCode.WRITE_CAPABILITY_NOT_READY,
                MutationStage.CREDENTIAL,
            )
        return material

    def __repr__(self) -> str:
        return f"WriteCapabilityBroker(capabilities={len(self._providers)})"

    __str__ = __repr__

    # ---- internals ----------------------------------------------------------

    def _not_ready(
        self,
        capability: WriteCapabilityId,
        state: CapabilityState,
        reason: MutationReasonCode,
    ) -> WriteCapabilityReadiness:
        return WriteCapabilityReadiness(
            capability_id=capability.value,
            provider=self._provider_name,
            state=state,
            reason=reason,
            permissions_attested=False,
        )


def _read_all(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _normalize(capability_id: object) -> str:
    if not isinstance(capability_id, str):
        return ""
    return capability_id.strip().lower()


__all__ = [
    "INTENDED_WRITE_PERMISSIONS",
    "WRITE_SECRET_NAMES",
    "FileWriteMaterialProvider",
    "WriteCapabilityBroker",
    "WriteCapabilityReadiness",
    "WriteMaterialProvider",
    "assert_read_write_disjoint",
    "intended_permissions",
    "is_write_capability",
    "parse_write_capability",
    "permission_failure",
    "write_capability_ids",
]
