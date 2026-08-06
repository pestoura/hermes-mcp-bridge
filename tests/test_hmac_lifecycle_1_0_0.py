"""Directed tests for the bounded 1.0.0 HMAC rotation lifecycle."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest

from hermes_mcp_bridge import signing

CURRENT_KEY = "current-signing-key-0123456789-abcdef"
PREVIOUS_KEY = "previous-signing-key-01234567-abcdef"
NOW = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)


def _digest(key: str, payload: str = "payload") -> str:
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _strict_rotation_env(*, valid_until: datetime | str | None) -> dict[str, str]:
    env = {
        "BRIDGE_SECURITY_MODE": "production",
        "HERMES_BRIDGE_HMAC_SECRET": CURRENT_KEY,
        "HERMES_BRIDGE_HMAC_KEY_ID": "current-2026-08",
        "HERMES_BRIDGE_HMAC_SECRET_PREVIOUS": PREVIOUS_KEY,
        "HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID": "previous-2026-07",
    }
    if isinstance(valid_until, datetime):
        env["HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL"] = (
            valid_until.astimezone(UTC).isoformat().replace("+00:00", "Z")
        )
    elif valid_until is not None:
        env["HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL"] = valid_until
    return env


def test_previous_signature_verifies_only_inside_strict_grace_window() -> None:
    env = _strict_rotation_env(valid_until=NOW + timedelta(hours=1))
    old_signature = _digest(PREVIOUS_KEY)

    posture = signing.signing_posture(env, now=NOW)

    assert posture.ok
    assert posture.previous_configured is True
    assert posture.previous_active is True
    assert posture.previous_verifier is True
    assert posture.previous_expired is False
    assert posture.previous_legacy_unbounded is False
    assert posture.previous_valid_until == "2026-08-06T01:00:00Z"
    assert signing.verify("payload", old_signature, env, now=NOW)


def test_previous_signature_is_rejected_at_exact_deadline() -> None:
    deadline = NOW + timedelta(minutes=30)
    env = _strict_rotation_env(valid_until=deadline)
    old_signature = _digest(PREVIOUS_KEY)

    posture = signing.signing_posture(env, now=deadline)

    assert posture.ok
    assert posture.previous_expired is True
    assert posture.previous_active is False
    assert posture.previous_verifier is False
    assert not signing.verify("payload", old_signature, env, now=deadline)


def test_expired_previous_key_does_not_block_current_signing() -> None:
    env = _strict_rotation_env(valid_until=NOW - timedelta(seconds=1))

    posture = signing.signing_posture(env, now=NOW)
    status, signature, key_id = signing.sign("payload", env, now=NOW)

    assert posture.ok
    assert posture.previous_expired is True
    assert posture.previous_active is False
    assert status == signing.SIGNATURE_ALGORITHM
    assert key_id == "current-2026-08"
    assert signature == _digest(CURRENT_KEY)
    assert signing.verify("payload", signature, env, now=NOW)
    assert not signing.verify("payload", _digest(PREVIOUS_KEY), env, now=NOW)


def test_strict_previous_key_without_deadline_is_fail_closed() -> None:
    env = _strict_rotation_env(valid_until=None)

    posture = signing.signing_posture(env, now=NOW)

    assert not posture.ok
    assert posture.previous_active is False
    assert "explicit validity deadline" in (posture.error or "")
    assert not signing.verify("payload", _digest(PREVIOUS_KEY), env, now=NOW)
    with pytest.raises(signing.SigningConfigError, match="validity deadline"):
        signing.sign("payload", env, now=NOW)


def test_relaxed_mode_preserves_legacy_fixture_but_reports_it() -> None:
    env = _strict_rotation_env(valid_until=None)
    env["BRIDGE_SECURITY_MODE"] = "test"

    posture = signing.signing_posture(env, now=NOW)

    assert posture.ok
    assert posture.previous_active is True
    assert posture.previous_legacy_unbounded is True
    assert posture.previous_valid_until is None
    assert signing.verify("payload", _digest(PREVIOUS_KEY), env, now=NOW)


@pytest.mark.parametrize(
    ("deadline", "message"),
    [
        ("2026-08-06T01:00:00", "must include a timezone"),
        ("not-a-time", "not valid ISO-8601"),
    ],
)
def test_invalid_previous_deadline_is_rejected(deadline: str, message: str) -> None:
    env = _strict_rotation_env(valid_until=deadline)

    posture = signing.signing_posture(env, now=NOW)

    assert not posture.ok
    assert message in (posture.error or "")
    assert posture.previous_active is False


def test_previous_deadline_cannot_exceed_seven_day_hard_limit() -> None:
    env = _strict_rotation_env(
        valid_until=NOW
        + timedelta(seconds=signing.MAX_PREVIOUS_GRACE_SECONDS + 1)
    )

    posture = signing.signing_posture(env, now=NOW)

    assert not posture.ok
    assert "maximum grace window" in (posture.error or "")
    assert posture.previous_active is False


def test_previous_deadline_at_seven_day_limit_is_allowed() -> None:
    deadline = NOW + timedelta(seconds=signing.MAX_PREVIOUS_GRACE_SECONDS)
    env = _strict_rotation_env(valid_until=deadline)

    posture = signing.signing_posture(env, now=NOW)

    assert posture.ok
    assert posture.previous_active is True
    assert posture.previous_valid_until == "2026-08-13T00:00:00Z"


def test_current_and_previous_key_material_must_differ() -> None:
    env = _strict_rotation_env(valid_until=NOW + timedelta(hours=1))
    env["HERMES_BRIDGE_HMAC_SECRET_PREVIOUS"] = CURRENT_KEY

    posture = signing.signing_posture(env, now=NOW)

    assert not posture.ok
    assert "keys must be different" in (posture.error or "")


def test_current_and_previous_key_ids_must_differ() -> None:
    env = _strict_rotation_env(valid_until=NOW + timedelta(hours=1))
    env["HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID"] = "current-2026-08"

    posture = signing.signing_posture(env, now=NOW)

    assert not posture.ok
    assert "identifiers must be different" in (posture.error or "")


def test_active_previous_key_requires_identifier_in_strict_mode() -> None:
    env = _strict_rotation_env(valid_until=NOW + timedelta(hours=1))
    del env["HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID"]

    posture = signing.signing_posture(env, now=NOW)

    assert not posture.ok
    assert "requires a key identifier" in (posture.error or "")


def test_dangling_previous_metadata_is_invalid() -> None:
    env = _strict_rotation_env(valid_until=NOW + timedelta(hours=1))
    del env["HERMES_BRIDGE_HMAC_SECRET_PREVIOUS"]

    posture = signing.signing_posture(env, now=NOW)

    assert not posture.ok
    assert "metadata configured without" in (posture.error or "")


def test_new_signatures_always_use_current_key_during_grace() -> None:
    env = _strict_rotation_env(valid_until=NOW + timedelta(hours=1))

    status, signature, key_id = signing.sign("payload", env, now=NOW)

    assert status == signing.SIGNATURE_ALGORITHM
    assert key_id == "current-2026-08"
    assert signature == _digest(CURRENT_KEY)
    assert signature != _digest(PREVIOUS_KEY)


def test_posture_is_non_sensitive_and_normalizes_offset_to_utc() -> None:
    env = _strict_rotation_env(valid_until="2026-08-06T02:30:00+01:00")

    summary = signing.signing_posture(env, now=NOW).summary()
    serialized = str(summary)

    assert summary["previous_valid_until"] == "2026-08-06T01:30:00Z"
    assert summary["previous_source_type"] == "env"
    assert CURRENT_KEY not in serialized
    assert PREVIOUS_KEY not in serialized
    assert "HERMES_BRIDGE_HMAC_SECRET" not in serialized


def test_naive_injected_clock_is_rejected_by_programming_contract() -> None:
    env = _strict_rotation_env(valid_until=NOW + timedelta(hours=1))

    with pytest.raises(ValueError, match="timezone-aware"):
        signing.signing_posture(env, now=datetime(2026, 8, 6, 0, 0))
