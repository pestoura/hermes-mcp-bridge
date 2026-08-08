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
    expected = Path(sys.executable).resolve()

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
