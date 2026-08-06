"""Static operational-contract tests for the 1.0.0 HMAC lifecycle."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_env_example_requires_both_previous_interval_boundaries() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "HERMES_BRIDGE_HMAC_PREVIOUS_VALID_FROM=" in text
    assert "HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL=" in text
    assert "interval cannot exceed" in text
    assert "seven days" in text


def test_hmac_runbook_pins_state_boundaries_and_cleanup() -> None:
    text = (ROOT / "docs" / "hmac-lifecycle-1.0.0.md").read_text(
        encoding="utf-8"
    )

    assert "start is inclusive" in text
    assert "end is exclusive" in text
    assert "previous_pending=true" in text
    assert "previous_expired=true" in text
    assert "HERMES_BRIDGE_HMAC_PREVIOUS_VALID_FROM" in text
    assert "HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL" in text
    assert "HERMES_BRIDGE_1_0_0_HMAC_LIFECYCLE_PASS" in text
    assert "single-slot acceptance" in text


def test_hard_grace_limit_is_not_environment_configurable() -> None:
    source = (ROOT / "src" / "hermes_mcp_bridge" / "signing.py").read_text(
        encoding="utf-8"
    )

    assert "MAX_PREVIOUS_GRACE_SECONDS = 7 * 24 * 60 * 60" in source
    assert "MAX_PREVIOUS_GRACE_SECONDS" not in (
        ROOT / ".env.example"
    ).read_text(encoding="utf-8")
