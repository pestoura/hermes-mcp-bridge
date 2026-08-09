"""Contract tests for an exact-SHA 1.0.0 candidate refresh."""

from pathlib import Path

DEPLOY_DIR = Path("deploy/1.0.0")
DOC = Path("docs/production-rollout-1.0.0.md")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_preflight_pins_versioned_exact_rollback_baseline() -> None:
    text = _read(DEPLOY_DIR / "preflight.sh")

    assert 'ROLLBACK_BRIDGE_VERSION="${ROLLBACK_BRIDGE_VERSION:-0.9.0}"' in text
    assert 'assert_image_id "$ROLLBACK_IMAGE" "$ROLLBACK_IMAGE_ID"' in text
    assert 'assert_image_version "$ROLLBACK_IMAGE" "$ROLLBACK_BRIDGE_VERSION"' in text
    assert '[ "$current_image_id" = "$ROLLBACK_IMAGE_ID" ]' in text
    assert "baseline de rollback" in text
    assert "corresponde ao ID imutavel 0.9.0" not in text


def test_same_version_rollback_keeps_full_1_0_security_validation() -> None:
    text = _read(DEPLOY_DIR / "rollback.sh")

    assert 'if [ "$ROLLBACK_BRIDGE_VERSION" = "$BRIDGE_VERSION" ]; then' in text
    assert "rollback_require_security=1" in text
    assert 'REQUIRE_1_0_SECURITY="$rollback_require_security"' in text
    assert 'assert_image_id "$ROLLBACK_IMAGE" "$ROLLBACK_IMAGE_ID"' in text
    assert 'assert_image_version "$ROLLBACK_IMAGE" "$ROLLBACK_BRIDGE_VERSION"' in text


def test_rollback_compose_requires_exact_accepted_image() -> None:
    text = _read(DEPLOY_DIR / "compose.rollback.yml")

    assert "ROLLBACK_IMAGE:?" in text
    assert "exact accepted rollback image" in text
    assert "known-good 0.9.0 image" not in text


def test_rollout_document_declares_bounded_candidate_refresh() -> None:
    text = _read(DOC)

    for token in (
        "Controlled `1.0.0 -> 1.0.0` refresh",
        "ROLLBACK_IMAGE_ID",
        "ROLLBACK_BRIDGE_VERSION=1.0.0",
        "exact currently running image ID",
        "dual mutation gate",
        "full `1.0` security posture",
        "must never convert repository `GREEN` into runtime `GREEN` by inference",
    ):
        assert token in text
