"""Behavioural tests for the 0.8.2 health-settle logic.

The 0.8.1 rollout scripts slept a fixed ``SETTLE_SECONDS=12`` before running
``validate.sh``. The container healthcheck declares ``start_period=10s`` and
``interval=30s`` with ``retries=3``, so the health status is still ``starting``
at 12s and validation raced the probe, producing a FALSE ROLLBACK. The 0.8.1
production rollout only succeeded with a manual ``SETTLE_SECONDS=60`` override.

0.8.2 replaces the fixed sleep with ``wait_for_health()``: a bounded poll of the
Docker health status with a budget derived from the container's own healthcheck
configuration. These tests drive that function against a fake ``docker`` binary
reproducing the scenarios from the defect report:

* ``starting`` past the legacy sleep and ``healthy`` before 60s => PASS
* ``unhealthy``                                                 => FAIL
* never leaves ``starting``                                     => FAIL (timeout)

Crucially, ``test_the_legacy_logic_would_fail_the_same_scenario`` runs the *old*
implementation against the *same* fake container and asserts it FAILS. Without
that differential, a regression back to a fixed sleep would still satisfy the
happy-path test and go unnoticed.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

DEPLOY_DIR = Path("deploy/0.8.2").resolve()
LIB = DEPLOY_DIR / "lib.sh"

#: The fixed sleep the 0.8.1 implementation used before checking health once.
LEGACY_FIXED_SLEEP = 12

#: How long the fake container reports ``starting`` before flipping to
#: ``healthy``. It MUST exceed ``LEGACY_FIXED_SLEEP``: if the container went
#: healthy at exactly 12s, the broken implementation would also pass and the
#: test would prove nothing. The defect report requires "starting for at least
#: 12s and healthy before 60s"; 18s satisfies both and stays discriminating.
STARTING_SECONDS = 18

#: Health knobs that must never leak from the ambient shell into a test run.
_AMBIENT_HEALTH_VARS = (
    "HEALTH_POLL_INTERVAL_SECONDS",
    "HEALTH_SETTLE_MARGIN_SECONDS",
    "HEALTH_SETTLE_MIN_SECONDS",
    "HEALTH_SETTLE_MAX_SECONDS",
    "HEALTH_SETTLE_SECONDS",
    "HEALTH_FALLBACK_START_PERIOD",
    "HEALTH_FALLBACK_INTERVAL",
    "HEALTH_FALLBACK_TIMEOUT",
    "HEALTH_FALLBACK_RETRIES",
    "HEALTH_REQUIRE_HEALTHCHECK",
    "CONTAINER_NAME",
)


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in _AMBIENT_HEALTH_VARS:
        env.pop(name, None)
    return env


def _fake_docker(
    tmp_path: Path,
    *,
    script_body: str,
    start_period_ns: int = 10_000_000_000,
    interval_ns: int = 30_000_000_000,
    timeout_ns: int = 5_000_000_000,
    retries: int = 3,
) -> Path:
    """Write a fake ``docker`` shim exposing a controllable health status."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'prev=""\n'
        'fmt=""\n'
        'for a in "$@"; do case "$prev" in --format) fmt="$a";; esac; prev="$a"; done\n'
        'case "$fmt" in\n'
        "  *Healthcheck*)\n"
        f'    printf "%s %s %s %s\\n" {start_period_ns} {interval_ns}'
        f" {timeout_ns} {retries}\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        f"{script_body}\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir


def _timed_health_docker(tmp_path: Path, starting_for: int) -> Path:
    """Fake docker reporting ``starting`` for N seconds, then ``healthy``."""

    marker = tmp_path / "started_at"
    marker.write_text(str(int(time.time())), encoding="utf-8")
    body = (
        f'start=$(cat "{marker}")\n'
        "now=$(date +%s)\n"
        f"if [ $(( now - start )) -lt {starting_for} ]; then\n"
        "  echo starting\n"
        "else\n"
        "  echo healthy\n"
        "fi\n"
        "exit 0\n"
    )
    return _fake_docker(tmp_path, script_body=body)


def _run_wait(
    bin_dir: Path,
    extra_env: dict[str, str] | None = None,
    budget: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the 0.8.2 ``wait_for_health`` against the fake docker on PATH."""

    probe = bin_dir.parent / "probe.sh"
    probe.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        f'. "{LIB}"\n'
        f'if wait_for_health "hermes-mcp-bridge" "{budget}"; then\n'
        "  echo RESULT_PASS\n"
        "else\n"
        "  echo RESULT_FAIL\n"
        "fi\n",
        encoding="utf-8",
    )
    env = _clean_env()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HEALTH_POLL_INTERVAL_SECONDS"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, env=env, check=False
    )


def _run_legacy_0_8_1(bin_dir: Path, tmp_path: Path, settle: int) -> bool:
    """Reproduce the 0.8.1 logic: sleep a fixed N, then read the status once.

    Returns True if the legacy logic would have accepted the deployment.
    """

    probe = tmp_path / f"legacy-{settle}.sh"
    probe.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        f"sleep {settle}\n"
        "hs=\"$(docker inspect hermes-mcp-bridge "
        "--format '{{.State.Health.Status}}')\"\n"
        'if [ "$hs" = "healthy" ]; then echo LEGACY_PASS; else echo LEGACY_FAIL; fi\n',
        encoding="utf-8",
    )
    env = _clean_env()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    out = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, env=env, check=False
    )
    return "LEGACY_PASS" in out.stdout


def test_starting_past_the_legacy_sleep_then_healthy_passes(tmp_path: Path) -> None:
    """The exact 0.8.1 false-rollback scenario must now PASS."""

    assert STARTING_SECONDS > LEGACY_FIXED_SLEEP, (
        "o fake tem de continuar em starting depois do sleep antigo, "
        "senao o codigo defeituoso tambem passaria"
    )
    bin_dir = _timed_health_docker(tmp_path, STARTING_SECONDS)

    began = time.monotonic()
    result = _run_wait(bin_dir)
    elapsed = time.monotonic() - began

    assert "RESULT_PASS" in result.stdout, result.stdout + result.stderr
    assert elapsed >= LEGACY_FIXED_SLEEP, (
        f"terminou em {elapsed:.1f}s, antes do sleep antigo de "
        f"{LEGACY_FIXED_SLEEP}s: nao reproduz o defeito"
    )
    assert elapsed < 60, f"demorou {elapsed:.1f}s: excedeu a janela util"


def test_the_legacy_logic_would_fail_the_same_scenario(tmp_path: Path) -> None:
    """Differential proof: the fix is real, not a bigger magic number.

    Same fake container as the test above, driven by the 0.8.1 implementation.
    It must FAIL — that failure *is* the production incident.
    """

    bin_dir = _timed_health_docker(tmp_path, STARTING_SECONDS)
    assert not _run_legacy_0_8_1(bin_dir, tmp_path, LEGACY_FIXED_SLEEP), (
        "a logica antiga passou: o cenario nao reproduz o falso rollback e os "
        "testes de health nao provam a correccao"
    )


def test_legacy_only_worked_with_the_manual_60s_override(tmp_path: Path) -> None:
    """Documents why the operator's SETTLE_SECONDS=60 workaround appeared to work."""

    bin_dir = _timed_health_docker(tmp_path, STARTING_SECONDS)
    assert _run_legacy_0_8_1(bin_dir, tmp_path, 20)


def test_unhealthy_fails_fast(tmp_path: Path) -> None:
    bin_dir = _fake_docker(tmp_path, script_body="echo unhealthy\nexit 0\n")
    began = time.monotonic()
    result = _run_wait(bin_dir)
    elapsed = time.monotonic() - began
    assert "RESULT_FAIL" in result.stdout, result.stdout + result.stderr
    assert elapsed < 10, "unhealthy deve falhar imediatamente, nao esperar o budget"


def test_permanent_starting_times_out_and_fails(tmp_path: Path) -> None:
    bin_dir = _fake_docker(tmp_path, script_body="echo starting\nexit 0\n")
    result = _run_wait(bin_dir, budget="4")
    assert "RESULT_FAIL" in result.stdout, result.stdout + result.stderr
    assert "timeout de health" in result.stderr


def test_starting_inside_the_window_is_not_a_failure(tmp_path: Path) -> None:
    """Regression guard for the defect: starting must never fail early."""

    bin_dir = _fake_docker(tmp_path, script_body="echo starting\nexit 0\n")
    result = _run_wait(bin_dir, budget="3")
    # It failed only because the budget expired, never because of "starting".
    assert "RESULT_FAIL" in result.stdout
    assert "unhealthy" not in result.stderr


def _budget(bin_dir: Path, tmp_path: Path, env: dict[str, str] | None = None) -> int:
    probe = tmp_path / "budget.sh"
    probe.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        f'. "{LIB}"\n'
        'health_settle_budget "hermes-mcp-bridge"\n',
        encoding="utf-8",
    )
    full = _clean_env()
    full["PATH"] = f"{bin_dir}:{full['PATH']}"
    if env:
        full.update(env)
    out = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, env=full, check=False
    )
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip())


def test_budget_is_derived_from_the_declared_healthcheck(tmp_path: Path) -> None:
    """budget = start_period + (interval + timeout) * retries + margin."""

    bin_dir = _fake_docker(tmp_path, script_body="echo starting\nexit 0\n")
    # 10 + (30 + 5) * 3 + 15 = 130
    assert _budget(bin_dir, tmp_path) == 130


def test_budget_tracks_a_different_healthcheck(tmp_path: Path) -> None:
    """Proof of derivation, not a constant: change the healthcheck, budget moves."""

    bin_dir = _fake_docker(
        tmp_path,
        script_body="echo starting\nexit 0\n",
        start_period_ns=20_000_000_000,
        interval_ns=10_000_000_000,
        timeout_ns=2_000_000_000,
        retries=4,
    )
    # 20 + (10 + 2) * 4 + 15 = 83
    assert _budget(bin_dir, tmp_path) == 83


def test_budget_is_larger_than_the_0_8_1_fixed_sleep(tmp_path: Path) -> None:
    """The whole point of 0.8.2: the derived budget dwarfs SETTLE_SECONDS=12."""

    bin_dir = _fake_docker(tmp_path, script_body="echo starting\nexit 0\n")
    assert _budget(bin_dir, tmp_path) > LEGACY_FIXED_SLEEP
    # It also covers the manual 60s override that 0.8.1 needed.
    assert _budget(bin_dir, tmp_path) >= 60


def test_budget_honours_the_configurable_floor_and_ceiling(tmp_path: Path) -> None:
    (tmp_path / "tiny").mkdir(exist_ok=True)
    tiny = _fake_docker(
        tmp_path / "tiny",
        script_body="echo starting\nexit 0\n",
        start_period_ns=1_000_000_000,
        interval_ns=1_000_000_000,
        timeout_ns=1_000_000_000,
        retries=1,
    )
    assert _budget(tiny, tmp_path, {"HEALTH_SETTLE_MIN_SECONDS": "45"}) == 45
    assert _budget(tiny, tmp_path, {"HEALTH_SETTLE_MAX_SECONDS": "5"}) == 5


def _no_healthcheck_docker(tmp_path: Path, name: str) -> Path:
    bin_dir = tmp_path / name / "bin"
    bin_dir.mkdir(parents=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'prev=""\n'
        'fmt=""\n'
        'for a in "$@"; do case "$prev" in --format) fmt="$a";; esac; prev="$a"; done\n'
        'case "$fmt" in *Healthcheck*) printf "\\n"; exit 0;; esac\n'
        "echo none\nexit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir


def test_budget_falls_back_when_no_healthcheck_declared(tmp_path: Path) -> None:
    bin_dir = _no_healthcheck_docker(tmp_path, "nohc")
    # Falls back to 10 + (30 + 5) * 3 + 15 = 130 via the HEALTH_FALLBACK_* knobs.
    assert _budget(bin_dir, tmp_path) == 130


def test_container_without_healthcheck_warns_but_does_not_fail(tmp_path: Path) -> None:
    bin_dir = _no_healthcheck_docker(tmp_path, "nohc2")
    result = _run_wait(bin_dir)
    assert "RESULT_PASS" in result.stdout
    assert "sem healthcheck declarado" in result.stderr


def test_no_healthcheck_fails_closed_when_required(tmp_path: Path) -> None:
    """The deploy path demands a real healthcheck; silence is not success."""

    bin_dir = _no_healthcheck_docker(tmp_path, "nohc3")
    result = _run_wait(bin_dir, {"HEALTH_REQUIRE_HEALTHCHECK": "1"})
    assert "RESULT_FAIL" in result.stdout, result.stdout + result.stderr
    assert "exigido para este caminho" in result.stderr


@pytest.mark.parametrize("script", ["deploy.sh", "rollback.sh"])
def test_mutating_paths_require_a_declared_healthcheck(script: str) -> None:
    text = (DEPLOY_DIR / script).read_text(encoding="utf-8")
    assert "export HEALTH_REQUIRE_HEALTHCHECK=1" in text, (
        f"{script} aceitaria um container sem healthcheck como sucesso"
    )


def test_missing_container_fails(tmp_path: Path) -> None:
    bin_dir = tmp_path / "gone" / "bin"
    bin_dir.mkdir(parents=True)
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    docker.chmod(0o755)
    result = _run_wait(bin_dir)
    assert "RESULT_FAIL" in result.stdout


@pytest.mark.parametrize("script", ["deploy.sh", "rollback.sh", "validate.sh"])
def test_scripts_use_wait_for_health_not_a_fixed_sleep(script: str) -> None:
    text = (DEPLOY_DIR / script).read_text(encoding="utf-8")
    assert "wait_for_health" in text, f"{script} nao usa wait_for_health"
    assert 'sleep "$SETTLE_SECONDS"' not in text, f"{script} mantem o sleep fixo"
    assert "SETTLE_SECONDS:-12" not in text, f"{script} mantem o default defeituoso"


def test_deploy_and_rollback_derive_the_tool_count_from_lib(tmp_path: Path) -> None:
    """External consumers must read the contract, not restate a literal count.

    ``lib.sh`` is the single shell-side declaration; deploy/rollback/validate
    must reference the variable rather than writing 27 again.
    """

    lib = (DEPLOY_DIR / "lib.sh").read_text(encoding="utf-8")
    assert 'export EXPECTED_TOOL_COUNT="27"' in lib

    for script in ("deploy.sh", "validate.sh"):
        text = (DEPLOY_DIR / script).read_text(encoding="utf-8")
        assert "EXPECTED_TOOL_COUNT" in text, f"{script} nao usa a constante"
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "=27" not in stripped.replace(" ", ""), (
                f"{script}:{lineno} recontagem literal de ferramentas: {stripped}"
            )


def test_shell_tool_count_matches_the_python_contract() -> None:
    """The shell constant and contracts.py must never drift apart."""

    from hermes_mcp_bridge.contracts import (
        CURRENT_CONTRACT_VERSION,
        SCHEMA_VERSION,
        expected_tool_count,
    )

    lib = (DEPLOY_DIR / "lib.sh").read_text(encoding="utf-8")
    assert f'export EXPECTED_TOOL_COUNT="{expected_tool_count()}"' in lib
    assert f'export BRIDGE_VERSION="{CURRENT_CONTRACT_VERSION}"' in lib
    assert f'export SCHEMA_VERSION="{SCHEMA_VERSION}"' in lib


def test_health_logs_never_expose_container_env(tmp_path: Path) -> None:
    """Sanitised logging: the wait loop must not inspect or print Env."""

    lib_text = LIB.read_text(encoding="utf-8")
    assert ".Config.Env" not in lib_text
    assert "{{json .Config}}" not in lib_text
    bin_dir = _fake_docker(tmp_path, script_body="echo healthy\nexit 0\n")
    result = _run_wait(bin_dir)
    combined = result.stdout + result.stderr
    for token in ("HERMES_API_KEY", "Env", "password", "token="):
        assert token not in combined, f"log expos: {token}"
