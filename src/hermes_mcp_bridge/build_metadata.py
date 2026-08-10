"""Canonical product build metadata for telemetry.

The bridge carries two different, deliberately separated identities:

* the **public contract identity** -- ``contract_version`` (``1.0.0``) and
  ``schema_version`` (``0.6.1``) -- which is frozen and gate-enforced, and
* the **product build identity** -- the release train (``2.0.1``) and the
  source revision the running artifact was built from.

``bridge_info`` keeps carrying the contract identity and therefore never
changes when a patch release ships. This module supplies the second identity
so operators can tell *which build* is serving that contract.

Resolution is canonical and automatic:

* ``release`` comes from :data:`PRODUCT_RELEASE`, the single in-tree source of
  truth (asserted against ``docs/changelog.md`` by the release gate), and may
  be overridden at deploy time by ``BRIDGE_PRODUCT_RELEASE``.
* ``revision`` comes from the build provenance already threaded through the
  image (``OCI_IMAGE_REVISION`` / ``BRIDGE_BUILD_REVISION``), normalized to the
  short SHA.

Both values are format-validated; anything unrecognised degrades to
``unknown`` rather than reaching the metrics registry as free text.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache

from .contracts import CURRENT_CONTRACT_VERSION, SCHEMA_VERSION

#: Canonical product release train of this source tree.
PRODUCT_RELEASE = "2.0.1"

#: Value used whenever a build attribute cannot be resolved safely.
UNKNOWN = "unknown"

#: Environment variables consulted for the build revision, in precedence order.
REVISION_ENV_VARS: tuple[str, ...] = ("BRIDGE_BUILD_REVISION", "OCI_IMAGE_REVISION")

#: Environment variable allowed to override the canonical release string.
RELEASE_ENV_VAR = "BRIDGE_PRODUCT_RELEASE"

#: Length of the short revision published as a metric label.
SHORT_REVISION_LENGTH = 7

_RELEASE_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]{1,32})?$")
_REVISION_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _read_env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def resolve_release() -> str:
    """Return the canonical product release, or ``unknown`` if malformed."""

    candidate = _read_env(RELEASE_ENV_VAR) or PRODUCT_RELEASE
    return candidate if _RELEASE_PATTERN.match(candidate) else UNKNOWN


def resolve_revision() -> str:
    """Return the short build revision, or ``unknown`` if unavailable."""

    for name in REVISION_ENV_VARS:
        candidate = _read_env(name)
        if _REVISION_PATTERN.match(candidate):
            return candidate.lower()[:SHORT_REVISION_LENGTH]
    return UNKNOWN


@dataclass(frozen=True)
class BuildMetadata:
    """Immutable, secret-free description of the running build."""

    release: str
    revision: str
    contract_version: str
    schema_version: str

    def as_labels(self) -> dict[str, str]:
        """Return the metric label mapping for this build."""

        return {
            "release": self.release,
            "revision": self.revision,
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
        }


def build_metadata() -> BuildMetadata:
    """Resolve build metadata from the canonical sources (uncached)."""

    return BuildMetadata(
        release=resolve_release(),
        revision=resolve_revision(),
        contract_version=CURRENT_CONTRACT_VERSION,
        schema_version=SCHEMA_VERSION,
    )


@lru_cache(maxsize=1)
def get_build_metadata() -> BuildMetadata:
    """Return the process-wide build metadata (resolved once)."""

    return build_metadata()


__all__ = [
    "PRODUCT_RELEASE",
    "RELEASE_ENV_VAR",
    "REVISION_ENV_VARS",
    "SHORT_REVISION_LENGTH",
    "UNKNOWN",
    "BuildMetadata",
    "build_metadata",
    "get_build_metadata",
    "resolve_release",
    "resolve_revision",
]
