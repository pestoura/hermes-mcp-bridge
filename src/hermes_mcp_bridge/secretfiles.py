"""Secret material loading from environment or mounted files.

Rules (applied uniformly):

* ``<NAME>_FILE`` takes precedence over ``<NAME>``. File-mounted secrets are the
  Docker-secrets friendly path and win over process environment.
* Values are read on demand and never cached, so rotation on disk takes effect
  without a restart and no secret lingers in module state.
* Trailing/leading whitespace and a single trailing newline are stripped.
* Only non-sensitive metadata (source type, length ok, key id) is ever exposed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

#: Minimum accepted secret length, overridable for constrained test setups.
MIN_SECRET_LENGTH_ENV = "BRIDGE_MIN_SECRET_LENGTH"
DEFAULT_MIN_SECRET_LENGTH = 32


def min_secret_length(env: Mapping[str, str] | None = None) -> int:
    environ = env if env is not None else os.environ
    raw = environ.get(MIN_SECRET_LENGTH_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_MIN_SECRET_LENGTH
    try:
        value = int(str(raw).strip())
    except ValueError:
        return DEFAULT_MIN_SECRET_LENGTH
    return max(1, value)


@dataclass(frozen=True)
class SecretSource:
    """Non-sensitive description of where a secret came from."""

    name: str
    configured: bool
    source_type: str  # file | env | none
    error: str | None = None

    def summary(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "source_type": self.source_type,
            "error": self.error,
        }


def _clean(value: str) -> str:
    return value.strip()


def read_secret(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """Return the secret value for ``name``: ``<NAME>_FILE`` first, then env."""

    environ = env if env is not None else os.environ
    path = environ.get(f"{name}_FILE")
    if path and path.strip():
        try:
            with open(path.strip(), encoding="utf-8") as handle:
                value = _clean(handle.read())
        except OSError:
            return None
        return value or None
    raw = environ.get(name)
    if raw is None:
        return None
    value = _clean(raw)
    return value or None


def describe_secret(name: str, env: Mapping[str, str] | None = None) -> SecretSource:
    """Describe a secret without revealing its value or full path."""

    environ = env if env is not None else os.environ
    path = environ.get(f"{name}_FILE")
    if path and path.strip():
        try:
            with open(path.strip(), encoding="utf-8") as handle:
                value = _clean(handle.read())
        except OSError:
            return SecretSource(name, False, "file", error="secret file unreadable")
        if not value:
            return SecretSource(name, False, "file", error="secret file is empty")
        return SecretSource(name, True, "file")
    raw = environ.get(name)
    if raw is None or not _clean(raw):
        return SecretSource(name, False, "none")
    return SecretSource(name, True, "env")
