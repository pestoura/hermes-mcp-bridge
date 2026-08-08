from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import hermes_mcp_bridge.v2.hermes_runtime as runtime


def test_resolver_uses_console_script_shebang_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    hermes.chmod(0o755)
    expected = Path(os.path.abspath(sys.executable))

    monkeypatch.setattr(
        runtime,
        "_supports_required_hermes_modules",
        lambda candidate, env: candidate == expected,
    )

    resolved = runtime.resolve_hermes_python(
        hermes,
        home=tmp_path,
        hermes_home=tmp_path,
        path_env=os.environ.get("PATH", ""),
    )

    assert resolved == expected


def test_resolver_discovers_managed_hermes_home_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_home = tmp_path / ".hermes"
    managed_python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
    managed_python.parent.mkdir(parents=True)
    managed_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    managed_python.chmod(0o755)

    wrapper = tmp_path / ".local" / "bin" / "hermes"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    expected = managed_python.resolve()

    monkeypatch.setattr(
        runtime,
        "_supports_required_hermes_modules",
        lambda candidate, env: candidate == expected,
    )

    resolved = runtime.resolve_hermes_python(
        wrapper,
        home=tmp_path,
        hermes_home=hermes_home,
        path_env=os.environ.get("PATH", ""),
    )

    assert resolved == expected


def test_resolver_preserves_managed_venv_python_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_home = tmp_path / ".hermes"
    managed_python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
    managed_python.parent.mkdir(parents=True)
    managed_python.symlink_to(Path(sys.executable).resolve())

    wrapper = tmp_path / ".local" / "bin" / "hermes"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)

    expected = Path(os.path.abspath(managed_python))
    dereferenced = managed_python.resolve()
    checked: list[Path] = []

    def supports(candidate: Path, env: dict[str, str]) -> bool:
        checked.append(candidate)
        return candidate == expected

    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", supports)

    resolved = runtime.resolve_hermes_python(
        wrapper,
        home=tmp_path,
        hermes_home=hermes_home,
        path_env=os.environ.get("PATH", ""),
    )

    assert resolved == expected
    assert resolved != dereferenced
    assert expected in checked


def test_resolver_prefers_managed_runtime_before_generic_path_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_home = tmp_path / ".hermes"
    managed_python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
    managed_python.parent.mkdir(parents=True)
    managed_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    managed_python.chmod(0o755)

    wrapper = tmp_path / "bin" / "hermes"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    expected = managed_python.resolve()
    checked: list[Path] = []

    def supports(candidate: Path, env: dict[str, str]) -> bool:
        checked.append(candidate)
        return candidate == expected

    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", supports)

    resolved = runtime.resolve_hermes_python(
        wrapper,
        home=tmp_path,
        hermes_home=hermes_home,
        path_env=os.environ.get("PATH", ""),
    )

    assert resolved == expected
    assert checked[0] == expected


def test_resolver_fails_closed_when_no_candidate_owns_hermes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/bin/sh\n", encoding="utf-8")
    hermes.chmod(0o755)
    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", lambda candidate, env: False)

    with pytest.raises(runtime.HermesRuntimeError) as excinfo:
        runtime.resolve_hermes_python(
            hermes,
            home=tmp_path,
            hermes_home=tmp_path,
            path_env=os.environ.get("PATH", ""),
        )

    assert excinfo.value.code == "HERMES_RUNTIME_PYTHON_UNRESOLVED"


def test_resolver_rejects_non_executable_console_script(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    hermes.chmod(0o600)

    with pytest.raises(runtime.HermesRuntimeError) as excinfo:
        runtime.resolve_hermes_python(
            hermes,
            home=tmp_path,
            hermes_home=tmp_path,
            path_env=os.environ.get("PATH", ""),
        )

    assert excinfo.value.code == "HERMES_RUNTIME_EXECUTABLE_INVALID"
