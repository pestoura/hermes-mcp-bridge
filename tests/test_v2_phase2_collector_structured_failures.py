"""Fail-closed reporting contract for the Phase 2 connected collector.

These tests pin the *diagnostic* behaviour only: any controlled failure of
``scripts/v2_phase2_direct_read_acceptance.py`` must produce exactly one
bounded, sanitized JSON document with ``gate = DIRECT_READ_BLOCKED`` and a
reason matching ``^[A-Z0-9_]{1,64}$``, and must never leak a path, an argument
value, an exception message or a traceback. The DIRECT/read-only functional
logic is not exercised or modified here.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "scripts" / "v2_phase2_direct_read_acceptance.py"
LAUNCHER = ROOT / "scripts" / "v2_phase2_connected_jarvas.sh"
REASON_PATTERN = re.compile(r"^[A-Z0-9_]{1,64}$")


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("phase2_collector", COLLECTOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before execution so dataclass annotation resolution can find
    # the module by name (slots=True re-creates the class).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def collector() -> Any:
    return _load()


def _run_main(collector: Any, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = collector.main(argv)
    return code, out.getvalue(), err.getvalue()


def _single_blocked(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line, got {len(lines)}"
    payload = json.loads(lines[0])
    assert payload["gate"] == "DIRECT_READ_BLOCKED"
    assert REASON_PATTERN.fullmatch(payload["reason"])
    return payload


# --------------------------------------------------------------------------
# reason sanitization / bounding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "github.get_repo",
        "/home/secret/path/state.db",
        "boom: unexpected 'value' at line 3",
        "ATTESTATION_missing-scope",
        "  ",
        "",
        None,
        123,
    ],
)
def test_sanitize_reason_always_bounded_and_stable(collector: Any, raw: Any) -> None:
    reason = collector.sanitize_reason(raw)
    assert REASON_PATTERN.fullmatch(reason)
    assert len(reason) <= collector.REASON_MAX_LENGTH


def test_sanitize_reason_is_deterministic(collector: Any) -> None:
    assert collector.sanitize_reason("a/b c") == collector.sanitize_reason("a/b c")


def test_sanitize_reason_strips_paths_and_punctuation(collector: Any) -> None:
    reason = collector.sanitize_reason("/home/estourpm/.hermes/state.db")
    assert "/" not in reason and "." not in reason
    assert REASON_PATTERN.fullmatch(reason)


def test_sanitize_reason_truncates_overlong_input(collector: Any) -> None:
    reason = collector.sanitize_reason("X" * 500)
    assert len(reason) == collector.REASON_MAX_LENGTH


def test_sanitize_reason_empty_degrades_to_unspecified(collector: Any) -> None:
    assert collector.sanitize_reason("///") == collector.UNSPECIFIED_REASON_TOKEN


def test_collector_error_code_is_normalized(collector: Any) -> None:
    exc = collector.CollectorError("DIRECT_FAILED_github.get_pr:404")
    assert REASON_PATTERN.fullmatch(exc.code)
    assert ":" not in exc.code and "." not in exc.code


def test_collector_error_never_exceeds_bound(collector: Any) -> None:
    exc = collector.CollectorError("SEMANTIC_MISMATCH_" + "z" * 200)
    assert len(exc.code) <= collector.REASON_MAX_LENGTH
    assert REASON_PATTERN.fullmatch(exc.code)


def test_foreign_token_bounds_unknown_provider_values(collector: Any) -> None:
    token = collector._foreign_token("HTTP 404 not-found /repos/x/y")
    assert REASON_PATTERN.fullmatch(token)
    assert len(token) <= 32


def test_tool_token_only_accepts_known_tool_ids(collector: Any) -> None:
    assert collector._tool_token("github.get_repo") == "GITHUB_GET_REPO"
    assert collector._tool_token("evil/../tool") == collector.UNSPECIFIED_REASON_TOKEN
    assert collector._tool_token(None) == collector.UNSPECIFIED_REASON_TOKEN


# --------------------------------------------------------------------------
# argparse / preconditions
# --------------------------------------------------------------------------


def test_argparse_failure_emits_single_sanitized_json(collector: Any) -> None:
    code, stdout, stderr = _run_main(collector, [])
    assert code == 2
    payload = _single_blocked(stdout)
    assert payload["reason"] == "ARGUMENTS_INVALID"
    assert stderr == ""


def test_argparse_bad_choice_does_not_echo_value(collector: Any) -> None:
    code, stdout, stderr = _run_main(
        collector,
        [
            "--targets",
            "/tmp/does-not-exist-targets.json",
            "--json-out",
            "/tmp/out.json",
            "--source-commit",
            "a" * 40,
            "--direct-core-commit",
            "b" * 40,
            "--provider-type",
            "SUPER-SECRET-VALUE",
            "--provider-attestation",
            "/tmp/att.json",
            "--hermes-state-db",
            "/tmp/state.db",
        ],
    )
    assert code == 2
    payload = _single_blocked(stdout)
    assert "SECRET" not in payload["reason"]
    assert stderr == ""


def test_argument_contract_error_is_a_systemexit(collector: Any) -> None:
    exc = collector.ArgumentContractError()
    assert isinstance(exc, SystemExit)
    assert exc.code == 2
    assert REASON_PATTERN.fullmatch(exc.reason)


def test_parser_raises_systemexit_without_stderr(collector: Any) -> None:
    parser = collector.build_parser()
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit):
        parser.parse_args([])
    assert err.getvalue() == ""


def _valid_argv(tmp_path: Path, **overrides: str) -> list[str]:
    targets = overrides.get("targets", str(tmp_path / "targets.json"))
    attestation = overrides.get("attestation", str(tmp_path / "att.json"))
    state_db = overrides.get("state_db", str(tmp_path / "state.db"))
    return [
        "--targets",
        targets,
        "--json-out",
        str(tmp_path / "out.json"),
        "--source-commit",
        "a" * 40,
        "--direct-core-commit",
        "b" * 40,
        "--provider-type",
        "github_app",
        "--provider-attestation",
        attestation,
        "--hermes-state-db",
        state_db,
    ]


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ("state_db", "HERMES_STATE_DB_NOT_A_FILE"),
        ("targets", "TARGETS_FILE_NOT_FOUND"),
        ("attestation", "PROVIDER_ATTESTATION_FILE_NOT_FOUND"),
    ],
)
def test_precondition_failures_are_stable_and_path_free(
    collector: Any, tmp_path: Path, missing: str, expected: str
) -> None:
    present = {"targets": "targets.json", "attestation": "att.json", "state_db": "state.db"}
    for key, name in present.items():
        if key == missing:
            continue
        (tmp_path / name).write_text("{}", encoding="utf-8")
    argv = _valid_argv(tmp_path)
    code, stdout, stderr = _run_main(collector, argv)
    assert code == 2
    payload = _single_blocked(stdout)
    assert payload["reason"] == expected
    assert str(tmp_path) not in stdout
    assert stderr == ""


# --------------------------------------------------------------------------
# SystemExit per phase / unexpected exceptions
# --------------------------------------------------------------------------


def _prepare_files(tmp_path: Path) -> None:
    for name in ("targets.json", "att.json", "state.db"):
        (tmp_path / name).write_text("{}", encoding="utf-8")


@pytest.mark.parametrize(
    "phase",
    [
        "PRECONDITION",
        "DIRECT_COLLECTION",
        "SHADOW_COLLECTION",
        "TOKEN_ACCOUNTING",
        "VALIDATION",
    ],
)
def test_systemexit_inside_phase_maps_to_sanitized_phase_reason(
    collector: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    _prepare_files(tmp_path)

    async def _boom(args: argparse.Namespace) -> dict[str, Any]:
        collector.enter_phase(phase)
        raise SystemExit(f"secret detail /home/{tmp_path.name}/state.db")

    monkeypatch.setattr(collector, "collect", _boom)
    code, stdout, stderr = _run_main(collector, _valid_argv(tmp_path))
    assert code == 2
    payload = _single_blocked(stdout)
    assert payload["reason"] == f"COLLECTOR_EXIT_{phase}"
    assert "secret" not in stdout.lower()
    assert stderr == ""


def test_systemexit_zero_is_not_swallowed(
    collector: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_files(tmp_path)

    async def _clean_exit(args: argparse.Namespace) -> dict[str, Any]:
        raise SystemExit(0)

    monkeypatch.setattr(collector, "collect", _clean_exit)
    with pytest.raises(SystemExit) as excinfo:
        _run_main(collector, _valid_argv(tmp_path))
    assert excinfo.value.code == 0


def test_unexpected_exception_reports_class_and_phase_only(
    collector: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_files(tmp_path)
    secret = "ghp_supersecrettoken /home/estourpm/.hermes/state.db"

    async def _boom(args: argparse.Namespace) -> dict[str, Any]:
        collector.enter_phase("SHADOW_COLLECTION")
        raise RuntimeError(secret)

    monkeypatch.setattr(collector, "collect", _boom)
    code, stdout, stderr = _run_main(collector, _valid_argv(tmp_path))
    assert code == 2
    payload = _single_blocked(stdout)
    assert payload["reason"] == "COLLECTOR_FAILURE_SHADOW_COLLECTION_RUNTIMEERROR"
    assert "ghp_" not in stdout
    assert "/home/" not in stdout
    assert "Traceback" not in stdout
    assert stderr == ""


def test_unexpected_exception_reason_stays_bounded(
    collector: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_files(tmp_path)

    long_cls = type("A" * 300, (RuntimeError,), {})

    async def _boom(args: argparse.Namespace) -> dict[str, Any]:
        collector.enter_phase("VALIDATION")
        raise long_cls("x")

    monkeypatch.setattr(collector, "collect", _boom)
    code, stdout, _ = _run_main(collector, _valid_argv(tmp_path))
    assert code == 2
    _single_blocked(stdout)


def test_collector_error_propagates_bounded_reason(
    collector: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_files(tmp_path)

    async def _boom(args: argparse.Namespace) -> dict[str, Any]:
        raise collector.CollectorError("DIRECT_FAILED_github.get_pr::403")

    monkeypatch.setattr(collector, "collect", _boom)
    code, stdout, stderr = _run_main(collector, _valid_argv(tmp_path))
    assert code == 2
    payload = _single_blocked(stdout)
    assert payload["reason"].startswith("DIRECT_FAILED_")
    assert stderr == ""


def test_enter_phase_rejects_unknown_phase(collector: Any) -> None:
    collector.enter_phase("NOT_A_PHASE")
    assert collector.current_phase() == collector.PHASE_PRECONDITION
    collector.enter_phase(collector.PHASE_VALIDATION)
    assert collector.current_phase() == "VALIDATION"
    collector.enter_phase(collector.PHASE_PRECONDITION)


# --------------------------------------------------------------------------
# no regression on the success path / schemas
# --------------------------------------------------------------------------


def test_success_path_still_writes_evidence_and_summary(
    collector: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_files(tmp_path)
    report = {
        "schema": collector.EVIDENCE_SCHEMA,
        "gate": collector.COLLECTION_GATE,
        "samples": [{"tool_id": "github.get_repo"}],
    }

    async def _ok(args: argparse.Namespace) -> dict[str, Any]:
        return report

    monkeypatch.setattr(collector, "collect", _ok)
    code, stdout, stderr = _run_main(collector, _valid_argv(tmp_path))
    assert code == 0
    payload = json.loads(stdout.strip())
    assert payload["gate"] == collector.COLLECTION_GATE
    assert payload["samples"] == 1
    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8")) == report
    assert stderr == ""


def test_success_schema_constants_unchanged(collector: Any) -> None:
    assert collector.EVIDENCE_SCHEMA == "hermes-v2-phase2-direct-read-acceptance/1"
    assert collector.COLLECTION_GATE == "DIRECT_READ_EVIDENCE_COLLECTED"
    assert collector.EXPECTED_SAMPLE_COUNT == 15


def test_collector_compiles() -> None:
    subprocess.run(
        [sys.executable, "-m", "py_compile", str(COLLECTOR)],
        check=True,
    )


# --------------------------------------------------------------------------
# launcher propagation / fallback
# --------------------------------------------------------------------------


def _launcher_text() -> str:
    return LAUNCHER.read_text(encoding="utf-8")


def test_launcher_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(LAUNCHER)], check=True)


def test_launcher_captures_only_sanitized_collector_reason() -> None:
    text = _launcher_text()
    assert "collector_output_field()" in text
    assert 'payload.get("gate") == "DIRECT_READ_BLOCKED"' in text
    assert 're.fullmatch(r"[A-Z0-9_]{1,64}", value)' in text
    assert "collector_output_field reason || true" in text
    assert 'blocked "$COLLECTOR_REASON"' in text
    assert "COLLECTOR_OUTPUT=''" in text


def test_launcher_discards_collector_stderr() -> None:
    text = _launcher_text()
    assert '--hermes-state-db "$SHADOW_HOME/state.db" 2>/dev/null' in text
    assert '--hermes-state-db "$SHADOW_HOME/state.db" >/dev/null' not in text


def test_launcher_abnormal_exit_has_distinct_stable_code() -> None:
    text = _launcher_text()
    assert 'blocked "CONNECTED_EVIDENCE_COLLECTION_ABNORMAL_EXIT"' in text
    assert 'blocked "CONNECTED_EVIDENCE_COLLECTION_FAILED"' not in text


def test_launcher_retains_no_raw_collector_artifacts() -> None:
    text = _launcher_text()
    assert "COLLECTOR_STDERR" not in text
    assert "collector.log" not in text


FRAGMENT_TEMPLATE = (
    "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -uo pipefail",
            'VENV_PY="__PY__"',
            "__HELPER__",
            "blocked() {",
            '  printf \'{"gate":"DIRECT_READ_BLOCKED","reason":"%s"}\\n\' "$1"',
            "  exit 2",
            "}",
            "COLLECTOR_OUTPUT=''",
            "COLLECTOR_STATUS=0",
            'COLLECTOR_OUTPUT="$("__FAKE__" 2>/dev/null)" || COLLECTOR_STATUS=$?',
            'if [[ "$COLLECTOR_STATUS" -ne 0 ]]; then',
            (
                "  COLLECTOR_REASON=\"$(printf '%s' \"$COLLECTOR_O"
                'UTPUT" | collector_output_field reason || true)"'
            ),
            "  COLLECTOR_OUTPUT=''",
            '  if [[ -n "$COLLECTOR_REASON" ]]; then',
            '    blocked "$COLLECTOR_REASON"',
            "  fi",
            '  blocked "CONNECTED_EVIDENCE_COLLECTION_ABNORMAL_EXIT"',
            "fi",
            "COLLECTOR_OUTPUT=''",
            "echo OK",
        ]
    )
    + "\n"
)


def _run_launcher_fragment(collector_stdout: str, exit_code: int, tmp_path: Path) -> str:
    """Exercise the launcher's capture/extract/propagate logic in isolation.

    The real ``collector_output_field`` helper is lifted verbatim out of the
    shipped launcher, so this test cannot drift from the implementation.
    """
    payload_file = tmp_path / "payload.txt"
    payload_file.write_text(collector_stdout, encoding="utf-8")

    fake = tmp_path / "fake_collector.sh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'cat "{payload_file}"\n'
        "printf 'leaky /home/estourpm/secret traceback\\n' >&2\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)

    text = _launcher_text()
    start = text.index("collector_output_field() {")
    marker = text.index('\' "$field" 2>/dev/null', start)
    end = text.index("}", marker) + 1
    helper = text[start:end].replace('"$VENV/bin/python"', '"$VENV_PY"')

    script = tmp_path / "fragment.sh"
    script.write_text(
        FRAGMENT_TEMPLATE.replace("__PY__", sys.executable)
        .replace("__HELPER__", helper)
        .replace("__FAKE__", str(fake)),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


def test_launcher_fragment_propagates_contract_reason(tmp_path: Path) -> None:
    out = _run_launcher_fragment(
        json.dumps({"gate": "DIRECT_READ_BLOCKED", "reason": "SHADOW_SESSION_ID_MISSING"}),
        2,
        tmp_path,
    )
    assert json.loads(out.strip())["reason"] == "SHADOW_SESSION_ID_MISSING"
    assert "/home/estourpm" not in out


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        json.dumps({"gate": "SOMETHING_ELSE", "reason": "X"}),
        json.dumps({"gate": "DIRECT_READ_BLOCKED", "reason": "lower case leak"}),
        json.dumps({"gate": "DIRECT_READ_BLOCKED", "reason": "A" * 200}),
        "",
    ],
)
def test_launcher_fragment_falls_back_on_invalid_contract(payload: str, tmp_path: Path) -> None:
    out = _run_launcher_fragment(payload, 1, tmp_path)
    assert json.loads(out.strip())["reason"] == "CONNECTED_EVIDENCE_COLLECTION_ABNORMAL_EXIT"
    assert "traceback" not in out.lower()
    assert "/home/estourpm" not in out
