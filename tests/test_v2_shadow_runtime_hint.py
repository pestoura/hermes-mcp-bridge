from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_mcp_bridge.v2.hermes_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "v2_phase2_connected_jarvas.sh"
PREPARE = ROOT / "scripts" / "v2_phase2_prepare_shadow_home.py"
PROBE = ROOT / "scripts" / "v2_phase2_probe_shadow_runtime.py"
RESOLVER = ROOT / "scripts" / "v2_resolve_hermes_python.py"

_FORBIDDEN_PATH_TOKEN = "/"


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# --- runtime roots vs probe environment --------------------------------------


def test_resolver_uses_real_roots_for_layout_and_shadow_env_for_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = tmp_path / "real"
    hermes_home = real_home / ".hermes"
    managed = _make_executable(hermes_home / "hermes-agent" / "venv" / "bin" / "python")
    shadow_home = tmp_path / "shadow"
    shadow_home.mkdir()

    wrapper = _make_executable(real_home / ".local" / "bin" / "hermes")
    expected = Path(os.path.abspath(managed))
    seen: list[tuple[Path, str, str]] = []

    def supports(candidate: Path, env: dict[str, str]) -> bool:
        seen.append((candidate, env["HOME"], env["HERMES_HOME"]))
        return candidate == expected

    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", supports)

    resolved = runtime.resolve_hermes_python(
        wrapper,
        home=real_home,
        hermes_home=hermes_home,
        path_env=os.environ.get("PATH", ""),
        probe_home=shadow_home,
        probe_hermes_home=shadow_home,
    )

    # Layout discovery came from the real managed roots ...
    assert resolved == expected
    # ... while the import proof ran under the shadow environment.
    assert seen
    assert all(home == str(shadow_home.resolve()) for _, home, _ in seen)
    assert all(hermes == str(shadow_home.resolve()) for _, _, hermes in seen)


def test_resolver_probe_env_defaults_to_real_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes = _make_executable(tmp_path / "hermes")
    captured: list[dict[str, str]] = []

    def supports(candidate: Path, env: dict[str, str]) -> bool:
        captured.append(env)
        return True

    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", supports)
    runtime.resolve_hermes_python(
        hermes, home=tmp_path, hermes_home=tmp_path, path_env=os.environ.get("PATH", "")
    )
    assert captured[0]["HOME"] == str(tmp_path.resolve())
    assert captured[0]["HERMES_HOME"] == str(tmp_path.resolve())


# --- explicit hint validation -------------------------------------------------


def test_valid_hint_preserves_venv_symlink_invocation_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(Path(sys.executable).resolve())
    expected = Path(os.path.abspath(venv_python))

    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", lambda candidate, env: True)

    resolved = runtime.validate_hermes_python_hint(
        venv_python,
        probe_home=tmp_path,
        probe_hermes_home=tmp_path,
        path_env=os.environ.get("PATH", ""),
    )

    assert resolved == expected
    assert resolved != venv_python.resolve()


@pytest.mark.parametrize(
    ("factory", "code"),
    [
        (lambda tmp: "relative/python", "HERMES_RUNTIME_PYTHON_HINT_NOT_ABSOLUTE"),
        (lambda tmp: "", "HERMES_RUNTIME_PYTHON_HINT_INVALID"),
        (lambda tmp: str(tmp / "missing" / "python"), "HERMES_RUNTIME_PYTHON_HINT_INVALID"),
    ],
)
def test_hint_fails_closed_for_bad_paths(
    tmp_path: Path, factory, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", lambda candidate, env: True)
    with pytest.raises(runtime.HermesRuntimeError) as excinfo:
        runtime.validate_hermes_python_hint(
            factory(tmp_path),
            probe_home=tmp_path,
            probe_hermes_home=tmp_path,
            path_env="",
        )
    assert excinfo.value.code == code
    assert _FORBIDDEN_PATH_TOKEN not in str(excinfo.value)


def test_hint_rejects_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "adir"
    target.mkdir()
    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", lambda candidate, env: True)
    with pytest.raises(runtime.HermesRuntimeError) as excinfo:
        runtime.validate_hermes_python_hint(
            target, probe_home=tmp_path, probe_hermes_home=tmp_path, path_env=""
        )
    assert excinfo.value.code == "HERMES_RUNTIME_PYTHON_HINT_NOT_EXECUTABLE"
    assert _FORBIDDEN_PATH_TOKEN not in str(excinfo.value)


def test_hint_rejects_non_executable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "python"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o600)
    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", lambda candidate, env: True)
    with pytest.raises(runtime.HermesRuntimeError) as excinfo:
        runtime.validate_hermes_python_hint(
            target, probe_home=tmp_path, probe_hermes_home=tmp_path, path_env=""
        )
    assert excinfo.value.code == "HERMES_RUNTIME_PYTHON_HINT_NOT_EXECUTABLE"


def test_hint_rejects_import_failing_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_executable(tmp_path / "python")
    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", lambda candidate, env: False)
    with pytest.raises(runtime.HermesRuntimeError) as excinfo:
        runtime.validate_hermes_python_hint(
            target, probe_home=tmp_path, probe_hermes_home=tmp_path, path_env=""
        )
    assert excinfo.value.code == "HERMES_RUNTIME_PYTHON_HINT_IMPORT_FAILED"
    assert _FORBIDDEN_PATH_TOKEN not in str(excinfo.value)


def test_hint_import_proof_runs_under_supplied_shadow_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_executable(tmp_path / "python")
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    captured: list[dict[str, str]] = []

    def supports(candidate: Path, env: dict[str, str]) -> bool:
        captured.append(env)
        return True

    monkeypatch.setattr(runtime, "_supports_required_hermes_modules", supports)
    runtime.validate_hermes_python_hint(
        target, probe_home=shadow, probe_hermes_home=shadow, path_env=""
    )
    assert captured[0]["HOME"] == str(shadow.resolve())
    assert captured[0]["HERMES_HOME"] == str(shadow.resolve())


# --- launcher / script wiring -------------------------------------------------


def test_launcher_resolves_runtime_before_shadow_transition_and_passes_hint() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    resolve_at = text.index("v2_resolve_hermes_python.py")
    prepare_at = text.index("v2_phase2_prepare_shadow_home.py")
    probe_at = text.index("v2_phase2_probe_shadow_runtime.py")
    shadow_start_at = text.index('"$HERMES_BIN" gateway run')

    assert resolve_at < prepare_at < shadow_start_at < probe_at
    assert '--hermes-python "$HERMES_PY"' in text
    assert text.count('--hermes-python "$HERMES_PY"') == 2
    # The legacy sibling guess is the blocker's root cause and must be gone.
    assert 'HERMES_PY="$(dirname "$HERMES_BIN")/python"' not in text
    assert 'blocked "HERMES_RUNTIME_PYTHON_UNRESOLVED"' in text


def test_launcher_never_exports_hint_or_real_home_into_shadow_env() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "HERMES_PYTHON=" not in text
    assert "export HERMES_PY" not in text
    assert 'HERMES_HOME="$HOME"' not in text
    assert 'HOME="$SOURCE_HERMES_HOME"' not in text
    for block in text.split("setsid env -i")[1:]:
        head = block.split("&\n")[0]
        assert "HERMES_PY" not in head
        assert "SOURCE_HERMES_HOME" not in head
        assert 'HOME="$SHADOW_HOME"' in head


def test_prepare_and_probe_validate_hint_fail_closed() -> None:
    prepare = PREPARE.read_text(encoding="utf-8")
    probe = PROBE.read_text(encoding="utf-8")
    assert "validate_hermes_python_hint(" in prepare
    assert 'parser.add_argument("--hermes-python", required=False, default=None)' in prepare
    assert "validate_hermes_python_hint(" in probe
    assert "_validated_hermes_runtime_python(args.hermes_python, shadow_home)" in probe
    # The probe must not rebuild managed-layout candidates from the shadow home.
    assert "resolve_hermes_python(" not in probe


def test_prepare_rejects_bad_hint_without_writing_artifacts(tmp_path: Path) -> None:
    source_home = tmp_path / "source"
    source_home.mkdir()
    (source_home / "config.yaml").write_text(
        "model:\n  provider: p\n  default: m\n", encoding="utf-8"
    )
    shadow_home = tmp_path / "shadow"
    api_key_out = tmp_path / "api.key"
    token = tmp_path / "token"
    token.write_text("x", encoding="utf-8")
    token.chmod(0o600)

    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE),
            "--source-home",
            str(source_home),
            "--shadow-home",
            str(shadow_home),
            "--mcp-python",
            sys.executable,
            "--mcp-script",
            str(PREPARE),
            "--token-file",
            str(token),
            "--repository",
            "owner/repo",
            "--api-port",
            "8123",
            "--api-key-out",
            str(api_key_out),
            "--hermes-python",
            "relative/python",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status"] == "SHADOW_HOME_BLOCKED"
    assert payload["reason"] == "HERMES_RUNTIME_PYTHON_HINT_NOT_ABSOLUTE"
    assert not shadow_home.exists()
    assert not api_key_out.exists()


def test_probe_rejects_bad_hint_without_writing_evidence(tmp_path: Path) -> None:
    shadow_home = tmp_path / "shadow"
    shadow_home.mkdir(mode=0o700)
    (shadow_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    api_key = tmp_path / "api.key"
    api_key.write_text("k\n", encoding="utf-8")
    api_key.chmod(0o600)
    json_out = tmp_path / "isolation.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--url",
            "http://127.0.0.1:1",
            "--api-key-file",
            str(api_key),
            "--repository",
            "owner/repo",
            "--source-commit",
            "0" * 40,
            "--json-out",
            str(json_out),
            "--hermes-python",
            "relative/python",
            "--shadow-home",
            str(shadow_home),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status"] == "SHADOW_ISOLATION_BLOCKED"
    assert not json_out.exists()


def test_resolver_helper_emits_only_reason_code_on_failure(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(RESOLVER),
            "--hermes-bin",
            str(tmp_path / "absent"),
            "--home",
            str(tmp_path),
            "--hermes-home",
            str(tmp_path),
            "--path-env",
            "",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.strip() == "HERMES_RUNTIME_EXECUTABLE_INVALID"


# --- untouched contracts ------------------------------------------------------


def test_shadow_isolation_payload_gains_no_new_keys() -> None:
    from hermes_mcp_bridge.v2 import shadow_isolation

    assert "hermes_python" not in shadow_isolation._ALLOWED_KEYS
    assert "hermes_runtime" not in shadow_isolation._ALLOWED_KEYS
    assert len(shadow_isolation._ALLOWED_KEYS) == 24
    probe_text = PROBE.read_text(encoding="utf-8")
    assert '"hermes_python":' not in probe_text


def test_hint_absent_preserves_legacy_prepare_behaviour() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    # Legacy console-script resolution must remain reachable when no hint is
    # supplied, so existing callers keep working unchanged.
    assert 'hermes_bin = shutil.which("hermes")' in text
    assert "resolve_hermes_python(" in text
    assert 'raise ShadowHomeError("HERMES_TOOLSET_RESOLVER_UNAVAILABLE")' in text
