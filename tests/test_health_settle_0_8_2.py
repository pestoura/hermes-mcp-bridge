"""Behavioural tests for the 0.8.2 health-settle logic.

The 0.8.1 rollout scripts slept a fixed ``SETTLE_SECONDS=12`` before running
``validate.sh``. The container healthcheck declares ``start_period=10s`` and
``interval=30s`` with ``retries=3``, so the health status is still ``starting``
at 12s and validation raced the probe, producing a FALSE ROLLBACK. The 0.8.1
production rollout only succeeded with a manual ``SETTLE_SECONDS=60`` override.

0.8.2 replaces the fixed sleep with ``wait_for_health()``: a bounded poll of the
Docker health status with a budget derived from the container's own healthcheck
configuration. These tests drive that function against a fake ``docker`` binary
that reproduces the exact scenarios the defect report requires:

* ``starting`` for at least 12 seconds and ``healthy`` before 60s  => PASS
* ``unhealthy``                                                    => FAIL
* never leaves ``starting``                                        => FAIL (timeout)
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

DEPLOY_DIR = Path("deploy/0.8.2").resolve()
LIB = DEPLOY_DIR / "lib.sh"

# Simulated clock is compressed: the real production window is
# start_period=10s + (interval=30s + timeout=5s) * 3 = 115s of budget. Driving a
# real 12s "starting" phase keeps the test faithful to the defect while the
# derived budget stays well above it.
STARTING_SECONDS = 12

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
        'if [ "$1" = "inspect" ] && [ "$2" = "--format" ]; then :; fi\n'
        'fmt=""\n'
        'for a in "$@"; do case "$prev" in --format) fmt="$a";; esac; prev="$a"; done\n'
        'case "$fmt" in\n'
        "  *Healthcheck*)\n"
        f'    printf "%s %s %s %s\\n" {start_period_ns} {interval_ns}'
        f' {timeout_ns} {retries}\n'
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        f"{script_body}\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir


def _run_wait(bin_dir: Path, extra_env: dict[str, str] | None = None,
              budget: str = "") -> subprocess.CompletedProcess[str]:
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


def test_starting_for_12s_then_healthy_before_60s_passes(tmp_path: Path) -> None:
    """The exact 0.8.1 false-rollback scenario must now PASS."""

    marker = tmp_path / "started_at"
    marker.write_text(str(int(time.time())), encoding="utf-8")
    body = (
        f'start=$(cat "{marker}")\n'
        "now=$(date +%s)\n"
        f'if [ $(( now - start )) -lt {STARTING_SECONDS} ]; then\n'
        '  echo starting\n'
        "else\n"
        '  echo healthy\n'
        "fi\n"
        "exit 0\n"
    )
    bin_dir = _fake_docker(tmp_path, script_body=body)

    began = time.monotonic()
    result = _run_wait(bin_dir)
    elapsed = time.monotonic() - began

    assert "RESULT_PASS" in result.stdout, result.stdout + result.stderr
    assert elapsed >= STARTING_SECONDS - 2, "nao aguardou a janela de starting"
    assert elapsed < 60, f"demorou {elapsed:.1f}s: excedeu a janela util"


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


def test_budget_is_larger_than_the_0_8_1_fixed_sleep(tmp_path: Path) -> None:
    """The whole point of 0.8.2: the derived budget dwarfs SETTLE_SECONDS=12."""

    bin_dir = _fake_docker(tmp_path, script_body="echo starting\nexit 0\n")
    assert _budget(bin_dir, tmp_path) > 12
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


def test_budget_falls_back_when_no_healthcheck_declared(tmp_path: Path) -> None:
    bin_dir = tmp_path / "nohc" / "bin"
    bin_dir.mkdir(parents=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$prev" in --format) fmt="$a";; esac; prev="$a"; done\n'
        'case "${fmt:-}" in *Healthcheck*) printf "\\n"; exit 0;; esac\n'
        "echo none\nexit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    # Falls back to 10 + (30 + 5) * 3 + 15 = 130 via the HEALTH_FALLBACK_* knobs.
    assert _budget(bin_dir, tmp_path) == 130


def test_container_without_healthcheck_warns_but_does_not_fail(tmp_path: Path) -> None:
    bin_dir = tmp_path / "nohc2" / "bin"
    bin_dir.mkdir(parents=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$prev" in --format) fmt="$a";; esac; prev="$a"; done\n'
        'case "${fmt:-}" in *Healthcheck*) printf "\\n"; exit 0;; esac\n'
        "echo none\nexit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    result = _run_wait(bin_dir)
    assert "RESULT_PASS" in result.stdout
    assert "sem healthcheck declarado" in result.stderr


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
