"""HMAC signing keys: current + previous (verify-only) with fail-closed posture.

Precedence for each key follows :mod:`hermes_mcp_bridge.secretfiles`:
``<NAME>_FILE`` (mounted Docker secret) wins over ``<NAME>`` (environment).
Values are read on demand and never cached.

The previous key is verification-only: it exists so a rotation grace period does
not invalidate manifests signed moments before the swap. It is never used to
produce new signatures.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass

from .policy import is_strict_mode, security_mode
from .secretfiles import describe_secret, min_secret_length, read_secret

CURRENT_SECRET_NAME = "HERMES_BRIDGE_HMAC_SECRET"
PREVIOUS_SECRET_NAME = "HERMES_BRIDGE_HMAC_SECRET_PREVIOUS"
CURRENT_KEY_ID_NAME = "HERMES_BRIDGE_HMAC_KEY_ID"
PREVIOUS_KEY_ID_NAME = "HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID"

SIGNATURE_ALGORITHM = "hmac-sha256"
UNSIGNED = "unsigned"


class SigningConfigError(Exception):
    """Signing configuration is invalid for the active security mode."""


@dataclass(frozen=True)
class SigningPosture:
    """Non-sensitive description of the signing configuration."""

    required: bool
    configured: bool
    source_type: str
    key_id: str | None
    previous_verifier: bool
    previous_key_id: str | None
    security_mode: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (self.configured or not self.required)

    def summary(self) -> dict[str, object]:
        return {
            "required": self.required,
            "configured": self.configured,
            "source_type": self.source_type,
            "key_id": self.key_id,
            "previous_verifier": self.previous_verifier,
            "previous_key_id": self.previous_key_id,
            "security_mode": self.security_mode,
            "error": self.error,
        }


def _key_id(name: str, env: Mapping[str, str] | None) -> str | None:
    import os

    environ = env if env is not None else os.environ
    value = environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def current_key(env: Mapping[str, str] | None = None) -> str | None:
    return read_secret(CURRENT_SECRET_NAME, env)


def previous_key(env: Mapping[str, str] | None = None) -> str | None:
    return read_secret(PREVIOUS_SECRET_NAME, env)


def signing_posture(env: Mapping[str, str] | None = None) -> SigningPosture:
    """Describe signing configuration; fail-closed detection lives here."""

    mode = security_mode(env)
    required = is_strict_mode(env)
    current_desc = describe_secret(CURRENT_SECRET_NAME, env)
    previous_desc = describe_secret(PREVIOUS_SECRET_NAME, env)
    value = current_key(env)
    minimum = min_secret_length(env)

    error: str | None = None
    if value is None:
        if required:
            error = "signing key required in this security mode but not configured"
    elif len(value) < minimum:
        error = "signing key shorter than the configured minimum length"

    previous_value = previous_key(env)
    if previous_value is not None and len(previous_value) < minimum:
        error = error or "previous signing key shorter than the configured minimum length"

    return SigningPosture(
        required=required,
        configured=value is not None and error is None,
        source_type=current_desc.source_type,
        key_id=_key_id(CURRENT_KEY_ID_NAME, env),
        previous_verifier=previous_value is not None and previous_desc.configured,
        previous_key_id=_key_id(PREVIOUS_KEY_ID_NAME, env),
        security_mode=mode,
        error=error,
    )


def assert_signing_ready(env: Mapping[str, str] | None = None) -> SigningPosture:
    posture = signing_posture(env)
    if not posture.ok:
        raise SigningConfigError(posture.error or "signing configuration invalid")
    return posture


def sign(payload: str, env: Mapping[str, str] | None = None) -> tuple[str, str, str | None]:
    """Sign ``payload``. Returns ``(status, signature, key_id)``.

    Raises :class:`SigningConfigError` in strict modes when no usable key is
    configured. In explicitly relaxed modes (``development``/``test``) the
    payload stays ``unsigned`` — reported, never silent.
    """

    posture = signing_posture(env)
    if posture.error:
        raise SigningConfigError(posture.error)
    value = current_key(env)
    if value is None:
        return UNSIGNED, "", None
    digest = hmac.new(value.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return SIGNATURE_ALGORITHM, digest, posture.key_id


def verify(payload: str, signature: str, env: Mapping[str, str] | None = None) -> bool:
    """Verify ``signature`` against current, then previous (grace) key."""

    if not signature:
        return False
    for value in (current_key(env), previous_key(env)):
        if not value:
            continue
        expected = hmac.new(
            value.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, signature):
            return True
    return False
