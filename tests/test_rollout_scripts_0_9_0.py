"""Safety and contract tests for the controlled 0.9.0 rollout bundle."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

DEPLOY_DIR = Path("deploy/0.9.0")
SCRIPTS = sorted(DEPLOY_DIR.glob("*.sh"))
COMPOSE_PROJECT = "hermes-mcp-bridge"

_AMBIENT_VARS = (
    "EXECUTE_DEPLOYMENT",
    "EXPECTED_SHA",
    "REQUIRED_SHA",
    "EXPECTED_SHA_0_9_0",
    "CANDIDATE_IMAGE",
    "ROLLBACK_IMAGE",
    "ROLLBACK_IMAGE_ID",
    "ROLLBACK_BRIDGE_VERSION",
    "ROLLBACK_TOOL_COUNT",
    "COMPOSE_FILE",
    "BACKUP_DIR",
    "STATE_DB",
    "MCP_PORT",
    "MCP_URL",
    "HEALTH_SETTLE_SECONDS",
    "EXPECT_BRIDGE_VERSION",
    "EXPECT_TOOL_COUNT",
    "EXPECT_SCHEMA_VERSION",
    "REQUIRE_0_9_SECURITY",
    "BRIDGE_ENV_FILE",
    "BRIDGE_STATE_DIR",
    "BRIDGE_POLICY_DIR",
    "BRIDGE_SECRETS_DIR",
    "BRIDGE_UID",
    "BRIDGE_GID",
    "BRIDGE_MIN_SECRET_LENGTH",
    "BRIDGE_METRICS_ENABLED",
    "BRIDGE_TRACING_ENABLED",
    "BRIDGE_TRACING_EXPORT",
    "BRIDGE_RETRY_ENABLED",
    "BRIDGE_CIRCUIT_ENABLED",
)


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in _AMBIENT_VARS:
        env.pop(name, None)
    return env


def _read(name: str) -> str:
    return (DEPLOY_DIR / name).read_text(encoding="utf-8")


def test_bundle_has_expected_files() -> None:
    assert {path.name for path in SCRIPTS} == {
        "deploy.sh",
        "lib.sh",
        "preflight.sh",
        "rollback.sh",
        "validate.sh",
    }
    assert (DEPLOY_DIR / "compose.candidate.yml").is_file()
    assert (DEPLOY_DIR / "compose.rollback.yml").is_file()


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_are_fail_fast_and_parse(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in text
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_fixed_contract_and_dual_mutation_gate() -> None:
    lib = _read("lib.sh")
    assert 'COMPOSE_PROJECT="hermes-mcp-bridge"' in lib
    assert 'BRIDGE_VERSION="0.9.0"' in lib
    assert 'SCHEMA_VERSION="0.6.1"' in lib
    assert 'EXPECTED_TOOL_COUNT="27"' in lib
    assert 'docker compose -p "$COMPOSE_PROJECT" -f "$file" "$@"' in lib
    assert '[ "${EXECUTE_DEPLOYMENT:-}" = "YES" ] || return 1' in lib
    assert '[ "${EXPECTED_SHA:-}" = "$required_sha" ] || return 1' in lib


def test_candidate_compose_is_fail_closed_and_non_public() -> None:
    text = _read("compose.candidate.yml")
    payload = yaml.safe_load(text)
    service = payload["services"]["hermes-mcp-bridge"]
    assert "ports" not in service
    assert service["network_mode"] == "host"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["environment"]["BRIDGE_SECURITY_MODE"] == "production"
    assert service["environment"]["BRIDGE_POLICY_PATH"].endswith("/production.json")
    assert service["environment"]["HERMES_API_KEY"] == ""
    assert service["environment"]["HERMES_API_KEY_FILE"] == "/run/secrets/hermes_api_key"
    assert service["environment"]["HERMES_BRIDGE_HMAC_SECRET"] == ""
    assert service["environment"]["HERMES_BRIDGE_HMAC_SECRET_PREVIOUS"] == ""
    assert (
        service["environment"]["HERMES_BRIDGE_HMAC_SECRET_FILE"]
        == "/run/secrets/hermes_bridge_hmac_secret"
    )
    assert service["environment"]["BRIDGE_POLICY_JSON"] == ""
    assert service["environment"]["BRIDGE_RETRY_ENABLED"] == "false"
    assert service["environment"]["BRIDGE_CIRCUIT_ENABLED"] == "false"
    assert set(service["secrets"]) == {
        "hermes_api_key",
        "hermes_bridge_hmac_secret",
    }
    assert any("/etc/hermes-mcp-bridge/policies:ro" in volume for volume in service["volumes"])
    assert service["logging"]["options"] == {"max-size": "10m", "max-file": "5"}


def test_rollback_compose_requires_explicit_image_and_preserves_legacy_env() -> None:
    text = _read("compose.rollback.yml")
    payload = yaml.safe_load(text)
    service = payload["services"]["hermes-mcp-bridge"]
    assert "ROLLBACK_IMAGE:?" in service["image"]
    assert "secrets" not in service
    assert "environment" not in service
    assert service["env_file"]


def test_preflight_requires_evidence_and_transitional_rollback_credential() -> None:
    text = _read("preflight.sh")
    for token in (
        'EXPECTED_SHA_0_9_0 obrigatorio',
        'ROLLBACK_IMAGE obrigatorio',
        'ROLLBACK_IMAGE_ID obrigatorio',
        'HERMES_API_KEY',
        'HERMES_BRIDGE_HMAC_KEY_ID',
        'assert_image_revision',
        'assert_image_version',
        'assert_image_id',
        'assert_secret_file',
        'validate_secret_lengths',
        'validate_policy_file',
    ):
        assert token in text
    for feature in (
        "BRIDGE_METRICS_ENABLED",
        "BRIDGE_TRACING_ENABLED",
        "BRIDGE_RETRY_ENABLED",
        "BRIDGE_CIRCUIT_ENABLED",
    ):
        assert feature in text


def test_validate_requires_full_0_9_security_posture() -> None:
    text = _read("validate.sh")
    for expression in (
        '.components.security_posture.status == "ready"',
        '.components.security_posture.policy.valid == true',
        '.components.security_posture.policy.source == "file"',
        '.components.security_posture.hmac.required == true',
        '.components.security_posture.hmac.configured == true',
        '.components.security_posture.hmac.source_type == "file"',
        ".components.config.api_key_configured == true",
    ):
        assert expression in text
    assert 'REQUIRE_SECURITY="${REQUIRE_0_9_SECURITY:-1}"' in text
    assert 'REQUIRED_TOOL="${REQUIRED_TOOL:-hermes_readiness}"' in text


def test_rollback_is_idempotent_and_never_reverts_sqlite() -> None:
    text = _read("rollback.sh")
    assert 'if [ "$current_image_id" = "$ROLLBACK_IMAGE_ID" ]; then' in text
    assert "--force-recreate" in text
    assert "running_image_id" in text
    assert "sqlite3" not in text
    assert " rm " not in text
    assert "REQUIRE_0_9_SECURITY=0" in text


def test_deploy_backs_up_state_and_requires_security_validation() -> None:
    text = _read("deploy.sh")
    assert 'python3 - "$STATE_DB" "$backup_path"' in text
    assert "src.backup(dst)" in text
    assert "REQUIRE_0_9_SECURITY=1" in text
    assert "candidate_image_id" in text
    assert "running_image_id" in text
    assert "--force-recreate" in text
    dry_index = text.index("DRY_RUN: nenhuma accao mutavel executada.")
    exit_index = text.index("exit 0", dry_index)
    mutation_index = text.index('compose "$COMPOSE_FILE" up -d')
    assert exit_index < mutation_index


def test_scripts_do_not_inspect_or_print_container_environment() -> None:
    forbidden = (
        ".Config.Env",
        "docker inspect --format '{{json .Config}}'",
        "BEGIN PRIVATE KEY",
        "Authorization: Bearer ",
    )
    for script in SCRIPTS:
        text = script.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text


def _find_shellcheck() -> str | None:
    found = shutil.which("shellcheck")
    if found:
        return found
    candidate = Path(sys.executable).parent / "shellcheck"
    return str(candidate) if candidate.exists() else None


@pytest.mark.skipif(_find_shellcheck() is None, reason="shellcheck ausente")
def test_shellcheck_clean() -> None:  # pragma: no cover - environment dependent
    shellcheck = _find_shellcheck()
    assert shellcheck is not None
    result = subprocess.run(
        [shellcheck, "-S", "warning", *[str(path) for path in SCRIPTS]],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def test_env_nonempty_rejects_empty_and_quoted_empty_values(tmp_path: Path) -> None:
    env_file = tmp_path / "values.env"
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        f'. "{(DEPLOY_DIR / "lib.sh").resolve()}"\n'
        'env_has_nonempty "$1" HERMES_API_KEY\n',
        encoding="utf-8",
    )
    for raw in ("", "   ", '\"\"', "''"):
        env_file.write_text(f"HERMES_API_KEY={raw}\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(probe), str(env_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0, raw

    env_file.write_text("HERMES_API_KEY=rollback-value\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(probe), str(env_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def _prepare_preflight_fixture(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.log"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then\n'
        '  case "$*" in\n'
        '    *org.opencontainers.image.revision*) echo "goodsha" ;;\n'
        '    *org.opencontainers.image.version*) echo "0.9.0" ;;\n'
        '    *"{{.Id}}"*) echo "sha256:rollback" ;;\n'
        '  esac\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$1" = "inspect" ]; then\n'
        '  case "$*" in\n'
        '    *"{{.Image}}"*) echo "sha256:rollback" ;;\n'
        '    *RestartCount*) echo "healthy|0" ;;\n'
        '    *) echo "healthy" ;;\n'
        '  esac\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$1" = "compose" ]; then\n'
        '  for arg in "$@"; do [ "$arg" = "config" ] && exit 0; done\n'
        '  exit 0\n'
        'fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    env_file = tmp_path / "bridge.env"
    env_file.write_text(
        "HERMES_API_KEY=legacy-rollback-key\n"
        "HERMES_BRIDGE_HMAC_KEY_ID=2026-08-key1\n"
        "BRIDGE_METRICS_ENABLED=0\n"
        "BRIDGE_RETRY_ENABLED=false\n"
        "BRIDGE_CIRCUIT_ENABLED=false\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.sqlite3").write_bytes(b"sqlite-placeholder")

    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "production.json").write_text(
        '{"name":"production","version":"0.9.0","read_only_actions":[],'
        '"mutating_actions":[],"deny_actions":[],"require_approval_actions":[],'
        '"unknown_action_decision":"DENY"}',
        encoding="utf-8",
    )

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    api_secret = secrets_dir / "hermes_api_key"
    hmac_secret = secrets_dir / "hermes_bridge_hmac_secret"
    api_secret.write_text("candidate-api-key\n", encoding="utf-8")
    hmac_secret.write_text("x" * 48 + "\n", encoding="utf-8")
    api_secret.chmod(0o600)
    hmac_secret.chmod(0o600)

    env = _clean_env()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["EXPECTED_SHA_0_9_0"] = "goodsha"
    env["CANDIDATE_IMAGE"] = "candidate:test"
    env["ROLLBACK_IMAGE"] = "rollback:test"
    env["ROLLBACK_IMAGE_ID"] = "sha256:rollback"
    env["BRIDGE_ENV_FILE"] = str(env_file)
    env["BRIDGE_STATE_DIR"] = str(state_dir)
    env["BRIDGE_POLICY_DIR"] = str(policy_dir)
    env["BRIDGE_SECRETS_DIR"] = str(secrets_dir)
    env["BRIDGE_UID"] = str(os.getuid())
    env["MIN_FREE_KB"] = "1"
    return env, calls


def test_preflight_and_deploy_dry_run_are_non_mutating(tmp_path: Path) -> None:
    env, calls = _prepare_preflight_fixture(tmp_path)
    preflight = subprocess.run(
        ["bash", str(DEPLOY_DIR / "preflight.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert preflight.returncode == 0, preflight.stderr
    assert "PREFLIGHT_0_9_0: GO" in preflight.stdout

    env["REQUIRED_SHA"] = "goodsha"
    deploy = subprocess.run(
        ["bash", str(DEPLOY_DIR / "deploy.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert deploy.returncode == 0, deploy.stderr
    assert "DEPLOY_0_9_0: DRY_RUN OK" in deploy.stdout
    logged = calls.read_text(encoding="utf-8")
    assert "up -d" not in logged
    assert "candidate-api-key" not in deploy.stdout + deploy.stderr
    assert "x" * 32 not in deploy.stdout + deploy.stderr


def test_preflight_rejects_permissive_secret_mode(tmp_path: Path) -> None:
    env, _ = _prepare_preflight_fixture(tmp_path)
    secret = Path(env["BRIDGE_SECRETS_DIR"]) / "hermes_bridge_hmac_secret"
    secret.chmod(0o644)
    result = subprocess.run(
        ["bash", str(DEPLOY_DIR / "preflight.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "0400 ou 0600" in result.stderr


def test_preflight_rejects_optional_features_during_base_rollout(tmp_path: Path) -> None:
    env, _ = _prepare_preflight_fixture(tmp_path)
    env_file = Path(env["BRIDGE_ENV_FILE"])
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "BRIDGE_RETRY_ENABLED=true\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(DEPLOY_DIR / "preflight.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "BRIDGE_RETRY_ENABLED" in result.stderr
