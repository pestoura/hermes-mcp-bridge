"""Safety and contract tests for the controlled 1.0.0 rollout bundle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

DEPLOY_DIR = Path("deploy/1.0.0")
SCRIPTS = sorted(DEPLOY_DIR.glob("*.sh"))
COMPOSE_PROJECT = "hermes-mcp-bridge"

_AMBIENT_VARS = (
    "EXECUTE_DEPLOYMENT",
    "EXPECTED_SHA",
    "REQUIRED_SHA",
    "EXPECTED_SHA_1_0_0",
    "CANDIDATE_IMAGE",
    "ROLLBACK_IMAGE",
    "ROLLBACK_IMAGE_ID",
    "ROLLBACK_BRIDGE_VERSION",
    "ROLLBACK_TOOL_COUNT",
    "COMPOSE_FILE",
    "BACKUP_DIR",
    "EVIDENCE_DIR",
    "STATE_DB",
    "SBOM_FILE",
    "SBOM_SHA256",
    "MCP_PORT",
    "MCP_URL",
    "HEALTH_SETTLE_SECONDS",
    "EXPECT_BRIDGE_VERSION",
    "EXPECT_TOOL_COUNT",
    "EXPECT_SCHEMA_VERSION",
    "REQUIRE_1_0_SECURITY",
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
    "CANDIDATE_REVISION",
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
    assert Path("docs/production-rollout-1.0.0.md").is_file()


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
    assert 'BRIDGE_VERSION="1.0.0"' in lib
    assert 'SCHEMA_VERSION="0.6.1"' in lib
    assert 'EXPECTED_TOOL_COUNT="27"' in lib
    assert 'docker compose -p "$COMPOSE_PROJECT" -f "$file" "$@"' in lib
    assert '[ "${EXECUTE_DEPLOYMENT:-}" = "YES" ] || return 1' in lib
    assert '[ "${EXPECTED_SHA:-}" = "$required_sha" ] || return 1' in lib


def test_candidate_compose_is_current_key_only_and_fail_closed() -> None:
    payload = yaml.safe_load(_read("compose.candidate.yml"))
    service = payload["services"]["hermes-mcp-bridge"]
    environment = service["environment"]

    assert "ports" not in service
    assert service["network_mode"] == "host"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert environment["BRIDGE_SECURITY_MODE"] == "production"
    assert environment["BRIDGE_POLICY_JSON"] == ""
    assert environment["BRIDGE_POLICY_PATH"].endswith("/production.json")
    assert environment["HERMES_API_KEY"] == ""
    assert environment["HERMES_API_KEY_FILE"] == "/run/secrets/hermes_api_key"
    assert environment["HERMES_BRIDGE_HMAC_SECRET"] == ""
    assert environment["HERMES_BRIDGE_HMAC_SECRET_FILE"].endswith(
        "/hermes_bridge_hmac_secret"
    )
    assert environment["HERMES_BRIDGE_HMAC_SECRET_PREVIOUS"] == ""
    assert environment["HERMES_BRIDGE_HMAC_SECRET_PREVIOUS_FILE"] == ""
    assert environment["HERMES_BRIDGE_HMAC_PREVIOUS_VALID_FROM"] == ""
    assert environment["HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL"] == ""
    assert environment["BRIDGE_METRICS_ENABLED"] == ""
    assert environment["BRIDGE_TRACING_ENABLED"] == ""
    assert environment["BRIDGE_TRACING_EXPORT"] == ""
    assert environment["BRIDGE_RETRY_ENABLED"] == "false"
    assert environment["BRIDGE_CIRCUIT_ENABLED"] == "false"
    assert set(service["secrets"]) == {
        "hermes_api_key",
        "hermes_bridge_hmac_secret",
    }
    assert service["logging"]["options"] == {"max-size": "10m", "max-file": "5"}


def test_rollback_compose_is_file_backed_and_requires_exact_image() -> None:
    payload = yaml.safe_load(_read("compose.rollback.yml"))
    service = payload["services"]["hermes-mcp-bridge"]
    environment = service["environment"]

    assert "ROLLBACK_IMAGE:?" in service["image"]
    assert environment["HERMES_API_KEY"] == ""
    assert environment["HERMES_API_KEY_FILE"] == "/run/secrets/hermes_api_key"
    assert environment["HERMES_BRIDGE_HMAC_SECRET"] == ""
    assert environment["HERMES_BRIDGE_HMAC_SECRET_FILE"].endswith(
        "/hermes_bridge_hmac_secret"
    )
    assert environment["BRIDGE_RETRY_ENABLED"] == "false"
    assert environment["BRIDGE_CIRCUIT_ENABLED"] == "false"
    assert set(service["secrets"]) == {
        "hermes_api_key",
        "hermes_bridge_hmac_secret",
    }


def test_preflight_pins_security_supply_chain_and_base_feature_state() -> None:
    text = _read("preflight.sh")
    for token in (
        "EXPECTED_SHA_1_0_0 obrigatorio",
        "ROLLBACK_IMAGE obrigatorio",
        "ROLLBACK_IMAGE_ID obrigatorio",
        "SBOM_FILE obrigatorio",
        "SBOM_SHA256 obrigatorio",
        "validate_sbom_evidence",
        "validate_state_db_read_only",
        "HERMES_API_KEY raw deve ser removida",
        "HERMES_BRIDGE_HMAC_KEY_ID",
        "previous HMAC secret deve ser removido",
        "validate_secret_lengths",
        "validate_policy_file",
        "HERMES_BRIDGE_1_0_0_PREFLIGHT_GO",
    ):
        assert token in text
    for feature in (
        "BRIDGE_METRICS_ENABLED",
        "BRIDGE_TRACING_ENABLED",
        "BRIDGE_TRACING_EXPORT",
        "BRIDGE_RETRY_ENABLED",
        "BRIDGE_CIRCUIT_ENABLED",
    ):
        assert feature in text


def test_validate_requires_full_1_0_posture_and_disabled_features() -> None:
    text = _read("validate.sh")
    for expression in (
        '.components.security_posture.status == "ready"',
        '.components.security_posture.policy.valid == true',
        '.components.security_posture.policy.source == "file"',
        '.components.security_posture.hmac.source_type == "file"',
        ".components.security_posture.hmac.previous_configured == false",
        ".components.security_posture.hmac.previous_active == false",
        ".components.security_posture.hmac.previous_pending == false",
        ".components.security_posture.hmac.previous_expired == false",
        ".bridge.observability.retry.enabled == false",
        ".bridge.observability.circuit_breaker.enabled == false",
        ".bridge.observability.metrics.enabled == false",
        ".bridge.observability.tracing.export_enabled == false",
    ):
        assert expression in text
    assert 'REQUIRE_SECURITY="${REQUIRE_1_0_SECURITY:-1}"' in text


def test_deploy_proves_backup_and_restore_before_mutation() -> None:
    text = _read("deploy.sh")
    assert "create_verified_backup" in text
    assert "verify_backup_restore_isolated" in text
    assert "BACKUP_SHA256" in text
    assert "REQUIRE_1_0_SECURITY=1" in text
    assert "candidate_image_id" in text
    assert "running_image_id" in text
    assert "--force-recreate" in text
    dry_index = text.index("DRY_RUN: nenhuma accao mutavel executada.")
    exit_index = text.index("exit 0", dry_index)
    mutation_index = text.index('compose "$COMPOSE_FILE" up -d')
    assert exit_index < mutation_index


def test_rollback_is_idempotent_and_never_reverts_sqlite() -> None:
    text = _read("rollback.sh")
    assert 'if [ "$current_image_id" = "$ROLLBACK_IMAGE_ID" ]; then' in text
    assert "--force-recreate" in text
    assert "running_image_id" in text
    assert "sqlite3" not in text
    assert " rm " not in text
    assert "ROLLBACK_BRIDGE_VERSION:-0.9.0" in text


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


def _write_state_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (10, '2026-08-06T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()


def _prepare_preflight_fixture(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.log"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then\n'
        '  image="$3"\n'
        '  case "$*" in\n'
        '    *org.opencontainers.image.revision*) [ "$image" = "candidate:test" ] && echo "goodsha" || echo "rollbacksha" ;;\n'
        '    *org.opencontainers.image.version*) [ "$image" = "candidate:test" ] && echo "1.0.0" || echo "0.9.0" ;;\n'
        '    *"{{.Id}}"*) [ "$image" = "candidate:test" ] && echo "sha256:candidate" || echo "sha256:rollback" ;;\n'
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
        "HERMES_BRIDGE_HMAC_KEY_ID=2026-08-key1\n"
        "BRIDGE_METRICS_ENABLED=0\n"
        "BRIDGE_TRACING_ENABLED=0\n"
        "BRIDGE_TRACING_EXPORT=0\n"
        "BRIDGE_RETRY_ENABLED=false\n"
        "BRIDGE_CIRCUIT_ENABLED=false\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_state_db(state_dir / "state.sqlite3")

    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "production.json").write_text(
        json.dumps(
            {
                "name": "production",
                "version": "1.0.0",
                "read_only_actions": [],
                "mutating_actions": [],
                "deny_actions": [],
                "require_approval_actions": [],
                "unknown_action_decision": "DENY",
            }
        ),
        encoding="utf-8",
    )

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    api_secret = secrets_dir / "hermes_api_key"
    hmac_secret = secrets_dir / "hermes_bridge_hmac_secret"
    api_secret.write_text("a" * 48 + "\n", encoding="utf-8")
    hmac_secret.write_text("b" * 48 + "\n", encoding="utf-8")
    api_secret.chmod(0o600)
    hmac_secret.chmod(0o600)

    sbom = tmp_path / "sbom.json"
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.7",
                "components": [{"type": "library", "name": "bridge"}],
            }
        ),
        encoding="utf-8",
    )

    env = _clean_env()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["EXPECTED_SHA_1_0_0"] = "goodsha"
    env["CANDIDATE_IMAGE"] = "candidate:test"
    env["ROLLBACK_IMAGE"] = "rollback:test"
    env["ROLLBACK_IMAGE_ID"] = "sha256:rollback"
    env["BRIDGE_ENV_FILE"] = str(env_file)
    env["BRIDGE_STATE_DIR"] = str(state_dir)
    env["BRIDGE_POLICY_DIR"] = str(policy_dir)
    env["BRIDGE_SECRETS_DIR"] = str(secrets_dir)
    env["BRIDGE_UID"] = str(os.getuid())
    env["MIN_FREE_KB"] = "1"
    env["SBOM_FILE"] = str(sbom)
    env["SBOM_SHA256"] = hashlib.sha256(sbom.read_bytes()).hexdigest()
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
    assert preflight.returncode == 0, preflight.stdout + preflight.stderr
    assert "HERMES_BRIDGE_1_0_0_PREFLIGHT_GO" in preflight.stdout

    env["REQUIRED_SHA"] = "goodsha"
    deploy = subprocess.run(
        ["bash", str(DEPLOY_DIR / "deploy.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert deploy.returncode == 0, deploy.stdout + deploy.stderr
    assert "DEPLOY_1_0_0: DRY_RUN OK" in deploy.stdout
    logged = calls.read_text(encoding="utf-8")
    assert "up -d" not in logged
    assert "a" * 32 not in deploy.stdout + deploy.stderr
    assert "b" * 32 not in deploy.stdout + deploy.stderr


def test_preflight_rejects_raw_api_key(tmp_path: Path) -> None:
    env, _ = _prepare_preflight_fixture(tmp_path)
    env_file = Path(env["BRIDGE_ENV_FILE"])
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "HERMES_API_KEY=raw-secret\n",
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
    assert "HERMES_API_KEY raw" in result.stderr
    assert "raw-secret" not in result.stdout + result.stderr


def test_preflight_rejects_previous_hmac_file(tmp_path: Path) -> None:
    env, _ = _prepare_preflight_fixture(tmp_path)
    previous = Path(env["BRIDGE_SECRETS_DIR"]) / "hermes_bridge_hmac_secret_previous"
    previous.write_text("c" * 48, encoding="utf-8")
    previous.chmod(0o600)
    result = subprocess.run(
        ["bash", str(DEPLOY_DIR / "preflight.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "previous HMAC secret" in result.stderr
    assert "c" * 32 not in result.stdout + result.stderr


def test_preflight_rejects_sbom_digest_mismatch(tmp_path: Path) -> None:
    env, _ = _prepare_preflight_fixture(tmp_path)
    env["SBOM_SHA256"] = "0" * 64
    result = subprocess.run(
        ["bash", str(DEPLOY_DIR / "preflight.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "SBOM SHA-256 mismatch" in result.stdout + result.stderr
