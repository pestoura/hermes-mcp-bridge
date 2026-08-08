"""Production-capable, secret-safe GitHub authorization provider for Phase 2.

This module is the *only* place in the V2 package that touches real GitHub
authorization material. It is deliberately separate from the readiness broker
(:mod:`hermes_mcp_bridge.v2.github_readiness`), which may only expose non-secret
status.

Security properties (all enforced here, all covered by tests):

* material is read from a **restricted file** referenced by ``<NAME>_FILE``,
  reusing the project-wide secret-file convention
  (:mod:`hermes_mcp_bridge.secretfiles`); a bare ``<NAME>`` environment value is
  rejected by default because it cannot carry file-permission guarantees;
* the file is read **on demand at the final boundary** only, never cached, so
  rotation on disk takes effect without a restart and no secret lingers in
  module state;
* file permissions are validated **on the open file descriptor** (``os.open``
  with ``O_NOFOLLOW``/``O_CLOEXEC`` then ``os.fstat`` on that same fd): regular
  file, no symlink, no group/other access. There is no ``lstat``-then-``open``
  window a symlink or inode substitution could slip through;
* classic/broad PATs are rejected. Only fine-grained tokens
  (``github_pat_``) and GitHub App installation tokens (``ghs_``) are accepted,
  and the observed material must match the declared provider type;
* neither the value nor the path ever appears in ``repr``/``str``, canonical
  payloads, results, errors or evidence. Denials are reported as stable,
  path-free status codes;
* every failure is fail-closed: ``resolve`` returns ``None`` and the executor
  denies with ``CREDENTIAL_MATERIAL_UNAVAILABLE``.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from collections.abc import Mapping
from enum import StrEnum, unique

from .github_auth import GitHubAuthorization
from .github_direct import GitHubRepositoryScope
from .github_registry import GITHUB_READ_CREDENTIAL_CAPABILITY

#: Default logical secret name. The provider reads ``<NAME>_FILE``.
DEFAULT_SECRET_NAME = "BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN"

#: Classic/broad PAT prefixes. Never accepted as Phase 2 evidence.
CLASSIC_PAT_PREFIXES = ("ghp_", "gho_")
#: Fine-grained personal access token prefix.
FINE_GRAINED_PREFIX = "github_pat_"
#: GitHub App installation access token prefix.
APP_INSTALLATION_PREFIX = "ghs_"

_MAX_MATERIAL_LENGTH = 512
_MIN_MATERIAL_LENGTH = 20

#: ``errno`` values a symlink triggers when ``O_NOFOLLOW`` is honoured.
_SYMLINK_ERRNOS = frozenset(
    value for value in (getattr(errno, name, None) for name in ("ELOOP", "EMLINK")) if value
)


@unique
class GitHubProviderType(StrEnum):
    """Provider types accepted by the Phase 2 least-privilege contract."""

    GITHUB_APP = "github_app"
    FINE_GRAINED_TOKEN = "fine_grained_token"


@unique
class MaterialClass(StrEnum):
    """Classification of observed authorization material."""

    GITHUB_APP = "github_app"
    FINE_GRAINED_TOKEN = "fine_grained_token"
    CLASSIC_PAT = "classic_pat"
    UNKNOWN = "unknown"


@unique
class AuthorizationStatus(StrEnum):
    """Stable, path-free and value-free provider status codes."""

    READY = "READY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    FILE_UNREADABLE = "FILE_UNREADABLE"
    FILE_EMPTY = "FILE_EMPTY"
    FILE_NOT_REGULAR = "FILE_NOT_REGULAR"
    FILE_PERMISSIONS_TOO_OPEN = "FILE_PERMISSIONS_TOO_OPEN"
    ENV_MATERIAL_REJECTED = "ENV_MATERIAL_REJECTED"
    MATERIAL_MALFORMED = "MATERIAL_MALFORMED"
    CLASSIC_PAT_REJECTED = "CLASSIC_PAT_REJECTED"
    PROVIDER_TYPE_MISMATCH = "PROVIDER_TYPE_MISMATCH"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    REPOSITORY_OUT_OF_SCOPE = "REPOSITORY_OUT_OF_SCOPE"


def classify_material(value: str) -> MaterialClass:
    """Classify authorization material without ever returning any of it."""
    if not isinstance(value, str):
        return MaterialClass.UNKNOWN
    if value.startswith(FINE_GRAINED_PREFIX):
        return MaterialClass.FINE_GRAINED_TOKEN
    if value.startswith(APP_INSTALLATION_PREFIX):
        return MaterialClass.GITHUB_APP
    if value.startswith(CLASSIC_PAT_PREFIXES):
        return MaterialClass.CLASSIC_PAT
    # Legacy 40-hex classic PATs carry no prefix at all.
    if len(value) == 40 and all(char in "0123456789abcdef" for char in value.lower()):
        return MaterialClass.CLASSIC_PAT
    return MaterialClass.UNKNOWN


def _expected_material_class(provider_type: GitHubProviderType) -> MaterialClass:
    if provider_type is GitHubProviderType.GITHUB_APP:
        return MaterialClass.GITHUB_APP
    return MaterialClass.FINE_GRAINED_TOKEN


class FileGitHubAuthorizationProvider:
    """Resolve ``github.read`` material from a restricted, file-mounted secret.

    The instance holds no secret state whatsoever: only the environment mapping,
    the logical secret name, the declared provider type and the repository
    allow-list.
    """

    __slots__ = (
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
        scope: GitHubRepositoryScope,
        provider_type: GitHubProviderType,
        secret_name: str = DEFAULT_SECRET_NAME,
        require_secure_mode: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(secret_name, str) or not secret_name.strip():
            raise ValueError("secret_name must be a non-empty string")
        if not isinstance(provider_type, GitHubProviderType):
            raise ValueError("provider_type must be a GitHubProviderType")
        self._scope = scope
        self._provider_type = provider_type
        self._secret_name = secret_name.strip()
        self._require_secure_mode = bool(require_secure_mode)
        self._env = env
        self._status = AuthorizationStatus.NOT_CONFIGURED
        self.resolve_calls = 0

    # ---- non-secret surface -------------------------------------------------

    @property
    def provider_type(self) -> GitHubProviderType:
        return self._provider_type

    @property
    def repository_scopes(self) -> tuple[str, ...]:
        return self._scope.repositories

    @property
    def last_status(self) -> AuthorizationStatus:
        """Last resolution/probe status. Never contains a value or a path."""
        return self._status

    def describe(self) -> dict[str, object]:
        """Return a non-secret description; contains no value and no path."""
        status = self.probe()
        return {
            "capability_id": GITHUB_READ_CREDENTIAL_CAPABILITY,
            "configured": status is AuthorizationStatus.READY,
            "provider_type": self._provider_type.value,
            "repository_scopes": list(self.repository_scopes),
            "secret_source_type": "file",
            "secure_mode_required": self._require_secure_mode,
            "status": status.value,
        }

    def __repr__(self) -> str:
        return (
            "FileGitHubAuthorizationProvider("
            f"provider_type={self._provider_type.value!r}, "
            f"repositories={len(self.repository_scopes)}, material=<redacted>)"
        )

    __str__ = __repr__

    # ---- boundary -----------------------------------------------------------

    def probe(self) -> AuthorizationStatus:
        """Validate configuration/material without returning it."""
        material, status = self._read_material()
        del material
        self._status = status
        return status

    def resolve(
        self,
        capability_id: str,
        repository: str,
    ) -> GitHubAuthorization | None:
        """Return bearer material for one capability and repository, or ``None``."""
        self.resolve_calls += 1

        if str(capability_id).strip().lower() != GITHUB_READ_CREDENTIAL_CAPABILITY:
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
        """Open, validate and read a secret file through a single file descriptor.

        There is deliberately **no** ``lstat``/``open`` pair here: the descriptor
        is obtained first with ``O_NOFOLLOW``/``O_CLOEXEC``/``O_NONBLOCK`` where
        supported, and every check (regular file, no group/other bits) runs with
        :func:`os.fstat` on that *same* descriptor. There is therefore no window
        in which the path can be swapped for a symlink or another inode between
        validation and read. Neither the path nor the value reaches the caller
        on any failure path.
        """
        flags = os.O_RDONLY
        for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"):
            flags |= getattr(os, name, 0)
        try:
            fd = os.open(target, flags)
        except OSError as exc:
            # ELOOP is what O_NOFOLLOW raises on a symlink; report it as a
            # non-regular file rather than a generic read error.
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
                raw = self._read_all(fd, _MAX_MATERIAL_LENGTH + 1)
            except OSError:
                return None, AuthorizationStatus.FILE_UNREADABLE
        finally:
            with contextlib.suppress(OSError):  # pragma: no cover - defensive
                os.close(fd)
        return raw, AuthorizationStatus.READY

    @staticmethod
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

    def _read_material(self) -> tuple[str | None, AuthorizationStatus]:
        environ = self._environ()
        path = environ.get(f"{self._secret_name}_FILE")
        if not path or not str(path).strip():
            if environ.get(self._secret_name):
                return None, AuthorizationStatus.ENV_MATERIAL_REJECTED
            return None, AuthorizationStatus.NOT_CONFIGURED

        target = str(path).strip()
        raw, status = self._read_fd_validated(target)
        if raw is None:
            return None, status
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None, AuthorizationStatus.MATERIAL_MALFORMED

        if not value:
            return None, AuthorizationStatus.FILE_EMPTY
        too_long = len(value) > _MAX_MATERIAL_LENGTH
        if too_long or len(value) < _MIN_MATERIAL_LENGTH or any(c.isspace() for c in value):
            return None, AuthorizationStatus.MATERIAL_MALFORMED

        observed = classify_material(value)
        if observed is MaterialClass.CLASSIC_PAT:
            return None, AuthorizationStatus.CLASSIC_PAT_REJECTED
        if observed is not _expected_material_class(self._provider_type):
            return None, AuthorizationStatus.PROVIDER_TYPE_MISMATCH
        return value, AuthorizationStatus.READY


__all__ = [
    "APP_INSTALLATION_PREFIX",
    "CLASSIC_PAT_PREFIXES",
    "DEFAULT_SECRET_NAME",
    "FINE_GRAINED_PREFIX",
    "AuthorizationStatus",
    "FileGitHubAuthorizationProvider",
    "GitHubProviderType",
    "MaterialClass",
    "classify_material",
]
