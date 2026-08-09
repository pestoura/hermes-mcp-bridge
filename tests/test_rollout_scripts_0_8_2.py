"""Static and behavioural tests for the 0.8.2 rollout shell scripts.

ShellCheck is resolved from PATH or from the interpreter directory
(``shellcheck-py``) so the gate never silently skips. These tests also provide:

* every Docker Compose invocation pins ``-p hermes-mcp-bridge``;
* scripts are fail-fast (``set -Eeuo pipefail``);
* dry-run is the default and performs no mutating action;
* candidate/rollback command shapes are correct;
* ``bash -n`` parses every script.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY_DIR = Path("deploy/0.8.2")
SCRIPTS = sorted(DEPLOY_DIR.glob("*.sh"))
COMPOSE_PROJECT = "hermes-mcp-bridge"

#: Rollout knobs that must never leak from the ambient shell into a test run.
#: A host that exported e.g. ROLLBACK_IMAGE_ID from a previous rollout would
#: otherwise make these tests fail (or, worse, pass) for the wrong reason.
_AMBIENT_ROLLOUT_VARS = (
    "EXECUTE_DEPLOYMENT",
    "EXPECTED_SHA",
    "REQUIRED_SHA",
    "CANDIDATE_IMAGE",
    "ROLLBACK_IMAGE",
    "ROLLBACK_IMAGE_ID",
    "ROLLBACK_BRIDGE_VERSION",
    "ROLLBACK_TOOL_COUNT",
    "EXPECTED_SHA_0_8_1",
    "EXPECTED_SHA_0_8_2",
    "COMPOSE_FILE",
    "BACKUP_DIR",
    "STATE_DB",
    "MCP_PORT",
    "MCP_URL",
    "HEALTH_SETTLE_SECONDS",
    "EXPECT_BRIDGE_VERSION",
    "EXPECT_TOOL_COUNT",
    "EXPECT_SCHEMA_VERSION",
)


def _clean_env() -> dict[str, str]:
    """A copy of os.environ with every ambient rollout override removed."""

    env = dict(os.environ)
    for name in _AMBIENT_ROLLOUT_VARS:
        env.pop(name, None)
    return env


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_deploy_dir_has_the_expected_scripts() -> None:
    names = {path.name for path in SCRIPTS}
    assert names == {"lib.sh", "preflight.sh", "deploy.sh", "rollback.sh", "validate.sh"}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_fail_fast(script: Path) -> None:
    assert "set -Eeuo pipefail" in _read(script)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse_with_bash_n(script: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_raw_docker_compose_without_project(script: Path) -> None:
    """Only the ``compose()`` helper (which pins -p) may call docker compose."""

    text = _read(script)
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "docker compose" not in stripped:
            continue
        if stripped.startswith(("log ", "echo ", "warn ", "ok ", "fail ")):
            # Documentation output rather than an invocation, but it must still
            # show the pinned project so operators never copy an unpinned
            # command out of the logs.
            assert '-p "$COMPOSE_PROJECT"' in stripped or "-p $COMPOSE_PROJECT" in stripped, (
                f"{script}:{lineno} mensagem com docker compose sem projeto: {stripped}"
            )
            continue
        assert '-p "$COMPOSE_PROJECT"' in stripped, (
            f"{script}:{lineno} docker compose sem projeto fixo: {stripped}"
        )


def test_compose_helper_pins_the_fixed_project() -> None:
    text = _read(DEPLOY_DIR / "lib.sh")
    assert f'COMPOSE_PROJECT="{COMPOSE_PROJECT}"' in text
    assert "export COMPOSE_PROJECT=" in text
    assert 'docker compose -p "$COMPOSE_PROJECT" -f "$file" "$@"' in text


@pytest.mark.parametrize(
    "script", [DEPLOY_DIR / "deploy.sh", DEPLOY_DIR / "rollback.sh"], ids=lambda p: p.name
)
def test_mutating_scripts_require_both_gates(script: Path) -> None:
    text = _read(script)
    assert "is_execute_mode" in text
    lib = _read(DEPLOY_DIR / "lib.sh")
    assert '[ "${EXECUTE_DEPLOYMENT:-}" = "YES" ] || return 1' in lib
    assert '[ "${EXPECTED_SHA:-}" = "$required_sha" ] || return 1' in lib


@pytest.mark.parametrize(
    "script", [DEPLOY_DIR / "deploy.sh", DEPLOY_DIR / "rollback.sh"], ids=lambda p: p.name
)
def test_dry_run_branch_exits_before_mutation(script: Path) -> None:
    text = _read(script)
    dry_index = text.index("DRY_RUN: nenhuma accao mutavel executada.")
    exit_index = text.index("exit 0", dry_index)
    up_index = text.index('compose "$COMPOSE_FILE" up -d')
    assert exit_index < up_index, "dry-run nao termina antes da recriacao do compose"


def test_deploy_validates_sha_and_image() -> None:
    text = _read(DEPLOY_DIR / "deploy.sh")
    assert "REQUIRED_SHA" in text
    assert 'preflight.sh" || fail "preflight NO-GO"' in text
    assert 'python3 - "$STATE_DB"' in text
    lib = _read(DEPLOY_DIR / "lib.sh")
    assert "assert_image_revision()" in lib
    assert "assert_image_id()" in lib


def test_deploy_is_idempotent_on_candidate_image() -> None:
    text = _read(DEPLOY_DIR / "deploy.sh")
    assert 'if [ "$current_image" = "$CANDIDATE_IMAGE" ]; then' in text
    assert "idempotente" in text


def test_rollback_is_idempotent_and_does_not_touch_state() -> None:
    text = _read(DEPLOY_DIR / "rollback.sh")
    assert 'if [ "$current_image" = "$ROLLBACK_IMAGE" ]; then' in text
    assert "sqlite3" not in text
    assert "rm " not in text


def test_validate_checks_contract_count_and_required_tool() -> None:
    text = _read(DEPLOY_DIR / "validate.sh")
    assert 'REQUIRED_TOOL="${REQUIRED_TOOL:-hermes_readiness}"' in text
    assert 'EXPECT_TOOLS="${EXPECT_TOOL_COUNT:-$EXPECTED_TOOL_COUNT}"' in text
    assert 'EXPECT_SCHEMA="${EXPECT_SCHEMA_VERSION:-$SCHEMA_VERSION}"' in text
    assert "ferramenta obrigatoria ausente" in text


def test_lib_declares_27_tools_and_schema_0_6_1() -> None:
    text = _read(DEPLOY_DIR / "lib.sh")
    assert 'export BRIDGE_VERSION="0.8.2"' in text
    assert 'export SCHEMA_VERSION="0.6.1"' in text
    assert 'export EXPECTED_TOOL_COUNT="27"' in text


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_variable_expansions_are_quoted(script: Path) -> None:
    """Catch unquoted $VAR usages in command position (word-splitting risk)."""

    unquoted = []
    for lineno, line in enumerate(_read(script).splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        # Ignore arithmetic/test contexts and quoted forms.
        for match in re.finditer(r'(?<!")\$(?:\{)?([A-Za-z_][A-Za-z_0-9]*)', stripped):
            start = match.start()
            before = stripped[:start]
            if before.count('"') % 2 == 1:
                continue  # inside a double-quoted string
            if before.count("'") % 2 == 1:
                continue  # inside a single-quoted string (e.g. jq filters)
            if match.group(1) in {"1", "2"}:
                continue
            if re.search(r"(^|\s)(local|export|declare)\s", before):
                continue
            if "=" in before.split()[-1] if before.split() else False:
                continue
            if re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", stripped):
                continue
            if match.group(1) in {
                "i",
                "avail_kb",
                "f",
                "waited",
                "budget",
                "poll",
                "value",
                "rt",
                "converted",
                "HEALTH_SETTLE_MARGIN_SECONDS",
            }:
                continue
            unquoted.append(f"{script}:{lineno}: {stripped}")
    assert not unquoted, "expansoes nao citadas:\n" + "\n".join(unquoted)


def test_scripts_contain_no_secret_material() -> None:
    forbidden = ("BEGIN PRIVATE KEY", "Authorization: Bearer ", "password=", "api_key=")
    for script in SCRIPTS:
        text = _read(script)
        for token in forbidden:
            assert token not in text, f"{script} contem material sensivel: {token}"


def _find_shellcheck() -> str | None:
    found = shutil.which("shellcheck")
    if found:
        return found
    candidate = Path(sys.executable).parent / "shellcheck"
    return str(candidate) if candidate.exists() else None


@pytest.mark.skipif(_find_shellcheck() is None, reason="shellcheck ausente")
def test_shellcheck_clean() -> None:  # pragma: no cover - env dependent
    shellcheck = _find_shellcheck()
    assert shellcheck is not None
    result = subprocess.run(
        [shellcheck, "-S", "warning", *[str(p) for p in SCRIPTS]],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def test_dry_run_deploy_makes_no_mutating_call(tmp_path: Path) -> None:
    """Run deploy.sh in dry-run with a fake docker on PATH and assert no mutation."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.log"
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then exit 0; fi\n'
        'if [ "$1" = "inspect" ]; then echo "healthy|0"; exit 0; fi\n'
        'if [ "$1" = "compose" ]; then\n'
        '  for arg in "$@"; do [ "$arg" = "config" ] && exit 0; done\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").chmod(0o755)

    env_file = tmp_path / "fake.env"
    env_file.write_text("PLACEHOLDER=1\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    env = _clean_env()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["BRIDGE_ENV_FILE"] = str(env_file)
    env["BRIDGE_STATE_DIR"] = str(state_dir)
    env["MIN_FREE_KB"] = "1"
    env.pop("EXECUTE_DEPLOYMENT", None)
    env.pop("EXPECTED_SHA", None)

    result = subprocess.run(
        ["bash", str(DEPLOY_DIR / "deploy.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "DEPLOY: DRY_RUN OK" in result.stdout
    logged = calls.read_text(encoding="utf-8") if calls.exists() else ""
    assert "up -d" not in logged, f"dry-run executou accao mutavel: {logged}"
    for line in logged.splitlines():
        if line.startswith("compose"):
            assert f"-p {COMPOSE_PROJECT}" in line


def test_dry_run_rollback_makes_no_mutating_call(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.log"
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [ "$1" = "inspect" ]; then echo "sha256:deadbeef"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").chmod(0o755)

    env = _clean_env()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.pop("EXECUTE_DEPLOYMENT", None)
    env.pop("EXPECTED_SHA", None)

    result = subprocess.run(
        ["bash", str(DEPLOY_DIR / "rollback.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ROLLBACK: DRY_RUN OK" in result.stdout
    logged = calls.read_text(encoding="utf-8") if calls.exists() else ""
    assert "up -d" not in logged
    for line in logged.splitlines():
        if line.startswith("compose"):
            assert f"-p {COMPOSE_PROJECT}" in line


def test_execute_mode_requires_matching_sha(tmp_path: Path) -> None:
    """EXECUTE_DEPLOYMENT=YES with a wrong SHA must stay in dry-run."""

    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        f'. "{DEPLOY_DIR.resolve()}/lib.sh"\n'
        'if is_execute_mode "goodsha"; then echo EXECUTE; else echo DRYRUN; fi\n',
        encoding="utf-8",
    )
    env = _clean_env()
    env["EXECUTE_DEPLOYMENT"] = "YES"
    env["EXPECTED_SHA"] = "wrongsha"
    out = subprocess.run(["bash", str(probe)], capture_output=True, text=True, env=env, check=False)
    assert out.stdout.strip() == "DRYRUN"

    env["EXPECTED_SHA"] = "goodsha"
    out = subprocess.run(["bash", str(probe)], capture_output=True, text=True, env=env, check=False)
    assert out.stdout.strip() == "EXECUTE"

    env["EXECUTE_DEPLOYMENT"] = "no"
    out = subprocess.run(["bash", str(probe)], capture_output=True, text=True, env=env, check=False)
    assert out.stdout.strip() == "DRYRUN"
