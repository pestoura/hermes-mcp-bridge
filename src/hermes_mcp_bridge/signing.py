"""HMAC signing keys with a bounded current/previous lifecycle.

Precedence for each key follows :mod:`hermes_mcp_bridge.secretfiles`:
``<NAME>_FILE`` (mounted Docker secret) wins over ``<NAME>`` (environment).
Values are read on demand and never cached.

The previous key is verification-only. In strict security modes it is accepted
only inside an explicit, timezone-aware validity interval. Expired previous
keys remain visible in the non-sensitive posture but are never used to verify a
signature and never prevent the current key from signing.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .policy import is_strict_mode, security_mode
from .secretfiles import describe_secret, min_secret_length, read_secret

CURRENT_SECRET_NAME = "HERMES_BRIDGE_HMAC_SECRET"
PREVIOUS_SECRET_NAME = "HERMES_BRIDGE_HMAC_SECRET_PREVIOUS"
CURRENT_KEY_ID_NAME = "HERMES_BRIDGE_HMAC_KEY_ID"
PREVIOUS_KEY_ID_NAME = "HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID"
PREVIOUS_VALID_FROM_NAME = "HERMES_BRIDGE_HMAC_PREVIOUS_VALID_FROM"
PREVIOUS_VALID_UNTIL_NAME = "HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL"

# A previous verifier is an emergency compatibility window, not a second
# long-lived production credential. Seven days is deliberately a hard code
# bound instead of another operator-overridable security control.
MAX_PREVIOUS_GRACE_SECONDS = 7 * 24 * 60 * 60

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
    previous_configured: bool = False
    previous_source_type: str = "none"
    previous_active: bool = False
    previous_pending: bool = False
    previous_expired: bool = False
    previous_valid_from: str | None = None
    previous_valid_until: str | None = None
    previous_legacy_unbounded: bool = False
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
            "previous_configured": self.previous_configured,
            "previous_source_type": self.previous_source_type,
            "previous_active": self.previous_active,
            "previous_pending": self.previous_pending,
            "previous_expired": self.previous_expired,
            "previous_valid_from": self.previous_valid_from,
            "previous_valid_until": self.previous_valid_until,
            "previous_legacy_unbounded": self.previous_legacy_unbounded,
            "security_mode": self.security_mode,
            "error": self.error,
        }


def _environ(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return env if env is not None else os.environ


def _key_id(name: str, env: Mapping[str, str] | None) -> str | None:
    value = _environ(env).get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _utc_now(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(
    name: str,
    label: str,
    env: Mapping[str, str] | None,
) -> tuple[datetime | None, str | None, str | None]:
    raw = _environ(env).get(name)
    if raw is None or not raw.strip():
        return None, None, None

    candidate = raw.strip()
    if candidate.endswith(("Z", "z")):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None, None, f"{label} is not valid ISO-8601"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, None, f"{label} must include a timezone"
    normalized = parsed.astimezone(UTC)
    return normalized, _format_utc(normalized), None


def current_key(env: Mapping[str, str] | None = None) -> str | None:
    return read_secret(CURRENT_SECRET_NAME, env)


def previous_key(env: Mapping[str, str] | None = None) -> str | None:
    return read_secret(PREVIOUS_SECRET_NAME, env)


def signing_posture(
    env: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> SigningPosture:
    """Describe signing configuration and enforce the previous-key interval."""

    mode = security_mode(env)
    required = is_strict_mode(env)
    current_desc = describe_secret(CURRENT_SECRET_NAME, env)
    previous_desc = describe_secret(PREVIOUS_SECRET_NAME, env)
    value = current_key(env)
    previous_value = previous_key(env)
    minimum = min_secret_length(env)
    current_id = _key_id(CURRENT_KEY_ID_NAME, env)
    previous_id = _key_id(PREVIOUS_KEY_ID_NAME, env)
    valid_from, valid_from_text, valid_from_error = _timestamp(
        PREVIOUS_VALID_FROM_NAME,
        "previous signing key validity start",
        env,
    )
    valid_until, valid_until_text, valid_until_error = _timestamp(
        PREVIOUS_VALID_UNTIL_NAME,
        "previous signing key validity deadline",
        env,
    )
    current_time = _utc_now(now)

    error: str | None = None

    def fail(message: str) -> None:
        nonlocal error
        if error is None:
            error = message

    current_configured = value is not None and len(value) >= minimum
    if value is None:
        if required:
            fail("signing key required in this security mode but not configured")
    elif len(value) < minimum:
        fail("signing key shorter than the configured minimum length")

    previous_configured = previous_value is not None and previous_desc.configured
    previous_active = False
    previous_pending = False
    previous_expired = False
    previous_legacy_unbounded = False

    if valid_from_error is not None:
        fail(valid_from_error)
    if valid_until_error is not None:
        fail(valid_until_error)

    has_window_metadata = valid_from is not None or valid_until is not None
    if not previous_configured:
        if has_window_metadata or previous_id is not None:
            fail("previous signing metadata configured without a previous signing key")
    else:
        if len(previous_value) < minimum:
            fail("previous signing key shorter than the configured minimum length")
        if value is not None and hmac.compare_digest(value, previous_value):
            fail("current and previous signing keys must be different")
        if current_id is not None and previous_id is not None and current_id == previous_id:
            fail("current and previous signing key identifiers must be different")

        if valid_from is None and valid_until is None:
            if required:
                fail("previous signing key requires an explicit validity interval")
            elif valid_from_error is None and valid_until_error is None:
                # Compatibility for explicitly relaxed development/test modes.
                # Production and security_required never take this path.
                previous_active = True
                previous_legacy_unbounded = True
        elif valid_from is None or valid_until is None:
            fail("previous signing key requires both validity start and deadline")
        elif valid_until <= valid_from:
            fail("previous signing key validity deadline must be after its start")
        elif valid_until - valid_from > timedelta(seconds=MAX_PREVIOUS_GRACE_SECONDS):
            fail("previous signing key validity interval exceeds the maximum grace window")
        elif current_time < valid_from:
            previous_pending = True
        elif current_time >= valid_until:
            previous_expired = True
        else:
            previous_active = True

        if required and (previous_active or previous_pending) and previous_id is None:
            fail("bounded previous signing key requires a key identifier")

    if error is not None:
        previous_active = False
        previous_pending = False

    return SigningPosture(
        required=required,
        configured=current_configured,
        source_type=current_desc.source_type,
        key_id=current_id,
        previous_verifier=previous_active,
        previous_key_id=previous_id,
        security_mode=mode,
        previous_configured=previous_configured,
        previous_source_type=previous_desc.source_type,
        previous_active=previous_active,
        previous_pending=previous_pending,
        previous_expired=previous_expired,
        previous_valid_from=valid_from_text,
        previous_valid_until=valid_until_text,
        previous_legacy_unbounded=previous_legacy_unbounded,
        error=error,
    )


def assert_signing_ready(
    env: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> SigningPosture:
    posture = signing_posture(env, now=now)
    if not posture.ok:
        raise SigningConfigError(posture.error or "signing configuration invalid")
    return posture


def sign(
    payload: str,
    env: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[str, str, str | None]:
    """Sign ``payload`` using only the current key."""

    posture = signing_posture(env, now=now)
    if posture.error:
        raise SigningConfigError(posture.error)
    value = current_key(env)
    if value is None:
        return UNSIGNED, "", None
    digest = hmac.new(value.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return SIGNATURE_ALGORITHM, digest, posture.key_id


def verify(
    payload: str,
    signature: str,
    env: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Verify against current and, only while active, previous key material."""

    if not signature:
        return False

    posture = signing_posture(env, now=now)
    if not posture.ok:
        return False

    value = current_key(env)
    if value:
        expected = hmac.new(
            value.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, signature):
            return True

    if not posture.previous_active:
        return False
    value = previous_key(env)
    if not value:
        return False
    expected = hmac.new(
        value.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
