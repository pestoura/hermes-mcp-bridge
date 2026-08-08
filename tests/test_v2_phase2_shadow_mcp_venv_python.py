"""Regression: the Phase 2 shadow MCP interpreter keeps its venv invocation path.

``Path.resolve()`` (and ``readlink -f``) dereference a virtualenv ``bin/python``
symlink to the base interpreter, which silently drops the venv site-packages
from the launched shadow MCP server. The launcher/helper must preserve the
absolute, non-dereferenced invocation path while still verifying fail-closed
that it is a regular executable file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from hermes_mcp_bridge.v2.hermes_runtime import absolute_invocation_path

ROOT = Path(__file__).resolve().parents[1]
PREPARE_PATH = ROOT / "scripts" / "v2_phase2_prepare_shadow_home.py"
LAUNCHER = ROOT / "scripts" / "v2_phase2_connected_jarvas.sh"


def _load_prepare_module():
    spec = importlib.util.spec_from_file_location(
        "v2_phase2_prepare_shadow_home_regression", PREPARE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREPARE = _load_prepare_module()


def _venv_python(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fake venv whose bin/python symlinks to a base interpreter."""
    base = tmp_path / "base" / "bin" / "python3.11"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    base.chmod(0o755)

    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.symlink_to(base)
    assert venv_python.is_symlink()
    assert venv_python.resolve() == base.resolve()
    return venv_python, base


def _prepare_args(tmp_path: Path, mcp_python: Path) -> argparse.Namespace:
    source_home = tmp_path / "source"
    source_home.mkdir(parents=True, exist_ok=True)
    (source_home / "config.yaml").write_text(
        "model:\n  provider: p\n  default: m\n", encoding="utf-8"
    )
    token = tmp_path / "token"
    token.write_text("x", encoding="utf-8")
    token.chmod(0o600)
    return argparse.Namespace(
        source_home=str(source_home),
        shadow_home=str(tmp_path / "shadow"),
        mcp_python=str(mcp_python),
        mcp_script=str(PREPARE_PATH),
        token_file=str(token),
        repository="owner/repo",
        api_port=8123,
        api_key_out=str(tmp_path / "api.key"),
        hermes_python=None,
    )


@pytest.fixture()
def stub_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real Hermes toolset resolver is not importable in CI; the interpreter
    # path handling under test is independent of it.
    monkeypatch.setattr(PREPARE, "_constrain_platform_to_shadow_mcp", lambda target: 0)


def test_absolute_invocation_path_does_not_dereference_venv_symlink(tmp_path: Path) -> None:
    venv_python, base = _venv_python(tmp_path)
    kept = absolute_invocation_path(venv_python)
    assert kept == venv_python
    assert kept != base
    assert kept.is_symlink()
    assert kept.is_absolute()


def test_prepare_writes_symlink_invocation_path_for_shadow_mcp(
    tmp_path: Path, stub_resolver: None
) -> None:
    venv_python, base = _venv_python(tmp_path)
    args = _prepare_args(tmp_path, venv_python)

    result = PREPARE.prepare(args)
    assert result["status"] == "SHADOW_HOME_PREPARED"

    config = yaml.safe_load((Path(args.shadow_home) / "config.yaml").read_text(encoding="utf-8"))
    command = config["mcp_servers"][PREPARE.SHADOW_MCP_SERVER]["command"]
    assert command == str(venv_python), "shadow MCP must be launched via the venv symlink"
    assert command != str(base), "venv python symlink must not be dereferenced"
    assert Path(command).is_absolute()
    assert Path(command).is_symlink()


def test_prepare_accepts_relative_venv_symlink_and_absolutises_it(
    tmp_path: Path, stub_resolver: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv_python, base = _venv_python(tmp_path)
    monkeypatch.chdir(venv_python.parent)
    args = _prepare_args(tmp_path, Path("python"))

    PREPARE.prepare(args)
    config = yaml.safe_load((Path(args.shadow_home) / "config.yaml").read_text(encoding="utf-8"))
    command = config["mcp_servers"][PREPARE.SHADOW_MCP_SERVER]["command"]
    assert command == str(venv_python)
    assert command != str(base)


def test_prepare_fails_closed_on_broken_symlink(tmp_path: Path, stub_resolver: None) -> None:
    broken = tmp_path / "venv" / "bin" / "python"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.symlink_to(tmp_path / "missing" / "python3")
    args = _prepare_args(tmp_path, broken)

    with pytest.raises(PREPARE.ShadowHomeError) as excinfo:
        PREPARE.prepare(args)
    assert excinfo.value.code == "MCP_PYTHON_INVALID"
    assert not Path(args.shadow_home).exists()


def test_prepare_fails_closed_on_non_executable_symlink_target(
    tmp_path: Path, stub_resolver: None
) -> None:
    venv_python, base = _venv_python(tmp_path)
    base.chmod(0o644)
    assert not os.access(venv_python, os.X_OK)
    args = _prepare_args(tmp_path, venv_python)

    with pytest.raises(PREPARE.ShadowHomeError) as excinfo:
        PREPARE.prepare(args)
    assert excinfo.value.code == "MCP_PYTHON_INVALID"
    assert not Path(args.shadow_home).exists()


def test_prepare_fails_closed_on_directory_path(tmp_path: Path, stub_resolver: None) -> None:
    directory = tmp_path / "venv" / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    args = _prepare_args(tmp_path, directory)

    with pytest.raises(PREPARE.ShadowHomeError) as excinfo:
        PREPARE.prepare(args)
    assert excinfo.value.code == "MCP_PYTHON_INVALID"


def test_blocker_payload_leaks_no_interpreter_path(tmp_path: Path) -> None:
    broken = tmp_path / "venv" / "bin" / "python"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.symlink_to(tmp_path / "missing" / "python3")
    args = _prepare_args(tmp_path, broken)

    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_PATH),
            "--source-home",
            args.source_home,
            "--shadow-home",
            args.shadow_home,
            "--mcp-python",
            str(broken),
            "--mcp-script",
            args.mcp_script,
            "--token-file",
            args.token_file,
            "--repository",
            args.repository,
            "--api-port",
            str(args.api_port),
            "--api-key-out",
            args.api_key_out,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status"] == "SHADOW_HOME_BLOCKED"
    # Whichever fail-closed gate fires first (runtime resolution in a host
    # without an installed Hermes, or the interpreter check itself), the
    # contract stays a stable path-free reason code.
    assert payload["reason"] in {
        "MCP_PYTHON_INVALID",
        "HERMES_TOOLSET_RESOLVER_UNAVAILABLE",
        "HERMES_RUNTIME_EXECUTABLE_MISSING",
        "HERMES_RUNTIME_PYTHON_UNRESOLVED",
    }
    assert set(payload) == {"status", "reason"}
    assert "/" not in json.dumps(payload)
    assert str(broken) not in completed.stdout


def test_launcher_passes_venv_symlink_without_dereferencing() -> None:
    launcher = LAUNCHER.read_text(encoding="utf-8")
    assert '--mcp-python "$VENV/bin/python"' in launcher
    # The MCP interpreter must never be normalised through readlink -f.
    assert 'readlink -f "$VENV/bin/python"' not in launcher
    assert 'realpath "$VENV/bin/python"' not in launcher


def test_prepare_source_does_not_resolve_mcp_python() -> None:
    source = PREPARE_PATH.read_text(encoding="utf-8")
    assert "absolute_invocation_path(args.mcp_python)" in source
    assert "Path(args.mcp_python).expanduser().resolve()" not in source


def test_prepare_still_enforces_regular_file_and_exec_bit() -> None:
    source = PREPARE_PATH.read_text(encoding="utf-8")
    assert "if not mcp_python.is_file() or not os.access(mcp_python, os.X_OK):" in source
    assert 'raise ShadowHomeError("MCP_PYTHON_INVALID")' in source
    # stat() follows the final symlink on purpose: the target must be a regular
    # executable file even though the invocation path is preserved.
    assert stat.S_ISREG(Path(sys.executable).stat().st_mode)
