"""Regression tests for secret source discovery and rotation.

No secret values are ever printed or asserted. We compare only digests
and lengths, and we exercise the fail-closed paths for empty/ambiguous
sources and long systemd unit names.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from hermes_mcp_bridge import secret_rotation as sr
from hermes_mcp_bridge.secret_rotation import (
    _read_env_file,
    _sha256_prefix,
    _short_unit_name,
    apply_rotation,
    plan_rotation,
    rollback_rotation,
)


def _write_env(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def test_read_env_file_skips_comments_and_blank() -> None:
    p = Path("__nonexistent_env_xyz__/nope.env")
    assert _read_env_file(str(p)) == {}
    tmp = Path("__test_env_1__.env")
    try:
        _write_env(tmp, "# comment\n\nAPI_SERVER_KEY=secret123\nHERMES_API_KEY='quoted'\n")
        env = _read_env_file(str(tmp))
        assert env["API_SERVER_KEY"] == "secret123"
        assert env["HERMES_API_KEY"] == "quoted"
    finally:
        tmp.unlink(missing_ok=True)


def test_discover_api_server_key_empty_fails_closed(monkeypatch) -> None:
    # simulate no systemd unit + no .env
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: None)
    monkeypatch.setattr(sr, "_read_env_file", lambda path: {})
    report = sr.discover_api_server_key()
    assert report.key == "API_SERVER_KEY"
    assert report.current_length == 0
    assert report.current_digest_prefix == ""
    assert report.comparable is False
    for s in report.sources:
        assert s["present"] is False


def test_discover_detects_source_mismatch(monkeypatch) -> None:
    # unit env and working dir .env disagree -> not comparable (all must match)
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "hermes.service")
    monkeypatch.setattr(
        sr,
        "_systemd_unit_environment",
        lambda unit: {"API_SERVER_KEY": "value-a"},
    )
    monkeypatch.setattr(sr, "_read_env_file", lambda path: {"API_SERVER_KEY": "value-b"})
    report = sr.discover_api_server_key()
    assert report.current_length == len("value-a")
    assert report.comparable is False


def test_discover_compares_only_digest(monkeypatch) -> None:
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: None)
    def fake_read(path):
        if path == ".env":
            return {"API_SERVER_KEY": "same-value"}
        return {}
    monkeypatch.setattr(sr, "_read_env_file", fake_read)
    report = sr.discover_api_server_key()
    assert report.comparable is True
    assert report.current_digest_prefix == _sha256_prefix("same-value")


def test_discover_hermes_api_key_compose_priority(monkeypatch) -> None:
    def fake_read(path):
        if path.endswith("compose/.env"):
            return {"HERMES_API_KEY": "compose-key"}
        return {}
    monkeypatch.setattr(sr, "_read_env_file", fake_read)
    report = sr.discover_hermes_api_key()
    assert report.current_length == len("compose-key")
    assert report.comparable is True


def test_plan_aborts_with_active_runs(monkeypatch) -> None:
    # health unreachable -> active_runs == -1 -> fail-closed abort without force
    monkeypatch.setattr(sr, "_active_api_runs", lambda: -1)
    with pytest.raises(RuntimeError):
        plan_rotation("API_SERVER_KEY", force=False)
    # force overrides
    plan = plan_rotation("API_SERVER_KEY", force=True)
    assert plan.active_runs == -1
    assert plan.dry_run is True


def test_plan_aborts_with_real_active_runs(monkeypatch) -> None:
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 4)
    with pytest.raises(RuntimeError):
        plan_rotation("API_SERVER_KEY", force=False)


def test_plan_health_unavailable_is_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(sr, "_active_api_runs", lambda: -1)
    with pytest.raises(RuntimeError):
        plan_rotation("API_SERVER_KEY")
    # force still allowed for operator override
    plan = plan_rotation("API_SERVER_KEY", force=True)
    assert plan.active_runs == -1


def test_plan_rejects_unsupported_key() -> None:
    with pytest.raises(ValueError):
        plan_rotation("NOT_A_KEY")


def test_apply_rotation_writes_atomically(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / ".env"
    _write_env(env, "OTHER=1\nAPI_SERVER_KEY=old\n")
    monkeypatch.chdir(tmp_path)
    plan = plan_rotation("API_SERVER_KEY", new_value="newsecret", force=True)
    plan = sr.RotationPlan(
        key=plan.key,
        new_value=plan.new_value,
        changed_paths=plan.changed_paths,
        backup_paths=plan.backup_paths,
        dry_run=False,
        requires_restart=plan.requires_restart,
        active_runs=plan.active_runs,
    )
    result = apply_rotation(plan)
    assert result.dry_run is False
    text = env.read_text()
    assert "API_SERVER_KEY=newsecret" in text
    assert "API_SERVER_KEY=old" not in text
    # backup preserved
    assert any(p.endswith(".pre-rotation") for p in result.backup_paths)
    backup = tmp_path / ".env.pre-rotation"
    assert backup.exists()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_apply_rejects_dry_run() -> None:
    plan = plan_rotation("API_SERVER_KEY", force=True)
    with pytest.raises(RuntimeError):
        apply_rotation(plan)


def test_rollback_restores_previous(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / ".env"
    _write_env(env, "API_SERVER_KEY=old\n")
    monkeypatch.chdir(tmp_path)
    plan = plan_rotation("API_SERVER_KEY", new_value="newsecret", force=True)
    plan = sr.RotationPlan(
        key=plan.key,
        new_value=plan.new_value,
        changed_paths=plan.changed_paths,
        backup_paths=plan.backup_paths,
        dry_run=False,
        requires_restart=plan.requires_restart,
        active_runs=plan.active_runs,
    )
    apply_rotation(plan)
    assert "API_SERVER_KEY=newsecret" in env.read_text()
    rollback_rotation(plan)
    assert "API_SERVER_KEY=old" in env.read_text()
    assert not (tmp_path / ".env.pre-rotation").exists()


def test_short_unit_name_enforces_55_chars() -> None:
    long_name = "hermes-mcp-bridge-gateway-rotation-very-long-unit-name-xyz"
    out = _short_unit_name(long_name)
    assert len(out) <= 55
    short = _short_unit_name("hermes.service")
    assert short == "hermes.service"


def test_verify_rotation_status_from_inspect(monkeypatch) -> None:
    # consistent single source -> verified, never leaks raw value
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: None)
    monkeypatch.setattr(sr, "_read_env_file", lambda path: {"API_SERVER_KEY": "x"})
    res = sr.verify_rotation("API_SERVER_KEY")
    assert res["key"] == "API_SERVER_KEY"
    assert res["status"] in ("verified", "inconclusive")
    # no raw value leaked: assert digest prefix only, never 'x'
    assert all(v != "x" for v in res.values())
    # also verify failure path: mismatch between sources -> inconclusive
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "hermes.service")
    monkeypatch.setattr(
        sr, "_systemd_unit_environment", lambda unit: {"API_SERVER_KEY": "x"}
    )
    monkeypatch.setattr(sr, "_read_env_file", lambda path: {"API_SERVER_KEY": "y"})
    res2 = sr.verify_rotation("API_SERVER_KEY")
    assert res2["status"] == "inconclusive"


def test_incident_wrong_n8n_env_source(monkeypatch) -> None:
    # regression: key present in n8n.env but not the effective gateway source
    def fake_read(path):
        if path.endswith("n8n.env"):
            return {"API_SERVER_KEY": "n8n-value"}
        return {}
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: None)
    monkeypatch.setattr(sr, "_read_env_file", fake_read)
    report = sr.discover_api_server_key()
    assert report.comparable is False  # effective source empty
    assert report.current_length == 0


def test_incident_empty_value_digest(monkeypatch) -> None:
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: None)
    monkeypatch.setattr(sr, "_read_env_file", lambda path: {"API_SERVER_KEY": ""})
    report = sr.discover_api_server_key()
    assert report.current_length == 0
    assert report.current_digest_prefix == ""
    assert report.comparable is False
