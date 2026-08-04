from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

import hermes_mcp_bridge._rotation_plans as rp
import hermes_mcp_bridge.secret_rotation as sr
from hermes_mcp_bridge.secret_rotation import (
    RotationPlan,
    apply_rotation,
    discover_api_server_key,
    discover_hermes_api_key,
    plan_rotation,
    rollback_rotation,
    schedule_external_restart,
)


def _fake_read(values: dict[str, str]):
    def _read(path):
        out = {}
        for k, v in values.items():
            out[k] = v
        return out

    return _read


def _fake_unit(name="hermes-gateway.service", working_dir=None, env=None):
    def _env(unit):
        if unit != name:
            return {}, [], None
        return (env or {}), [], working_dir

    return _env


def test_discover_insufficient_with_single_source():
    rep = discover_api_server_key(working_directory=None)
    assert rep.status == "insufficient"
    assert rep.comparable is False


def test_discover_consistent_with_two_matching_sources(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    env = workdir / ".env"
    env.write_text("API_SERVER_KEY=value-a\n")
    fu = _fake_unit(env={"API_SERVER_KEY": "value-a"}, working_dir=str(workdir))
    monkeypatch.setattr(sr, "_systemd_unit_environment", fu)
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "hermes-gateway.service")
    rep = discover_api_server_key()
    assert rep.status == "consistent"
    assert rep.comparable is True
    # absolute source path present
    wd = next(s for s in rep.sources if s["source"] == "working_dir_env")
    assert wd["path"].startswith("/")


def test_discover_mismatch_detected(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text("API_SERVER_KEY=value-a\n")
    fu = _fake_unit(env={"API_SERVER_KEY": "value-b"}, working_dir=str(workdir))
    monkeypatch.setattr(sr, "_systemd_unit_environment", fu)
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "hermes-gateway.service")
    rep = discover_api_server_key()
    assert rep.status == "mismatch"


def test_discover_no_relative_path_read(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: None)
    rep = discover_api_server_key(working_directory=None)
    # no absolute base -> insufficient, no relative file read
    assert rep.status == "insufficient"


def test_hermes_insufficient_with_single_source(tmp_path, monkeypatch):
    rep = discover_hermes_api_key(compose_dir=None)
    assert rep.status == "insufficient"


def test_hermes_consistent(tmp_path, monkeypatch):
    comp = tmp_path / "compose"
    comp.mkdir()
    (comp / ".env").write_text("HERMES_API_KEY=secret123\n")
    rep = discover_hermes_api_key(compose_dir=str(comp))
    assert rep.status == "consistent"
    assert all(s["path"].startswith("/") for s in rep.sources if s["path"])


def test_plan_transports_absolute_paths(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text("API_SERVER_KEY=old\n")
    fu = _fake_unit(env={"API_SERVER_KEY": "old"}, working_dir=str(workdir))
    monkeypatch.setattr(sr, "_systemd_unit_environment", fu)
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "hermes-gateway.service")
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 0)
    plan = plan_rotation("API_SERVER_KEY", "newsecret", working_directory=str(workdir))
    assert plan.plan_token
    assert plan.nonce
    assert plan.operation_id
    for p in plan.changed_paths:
        assert p.startswith("/")
    for b in plan.backup_paths:
        assert b.startswith("/")
        assert ".pre-rotation-" + plan.operation_id in b


def test_plan_rejects_when_no_absolute_source(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: None)
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 0)
    with pytest.raises(RuntimeError):
        plan_rotation("API_SERVER_KEY", "newsecret")


def test_plan_token_not_derivable_from_new_value(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text("API_SERVER_KEY=old\n")
    fu = _fake_unit(env={"API_SERVER_KEY": "old"}, working_dir=str(workdir))
    monkeypatch.setattr(sr, "_systemd_unit_environment", fu)
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "hermes-gateway.service")
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 0)
    plan = plan_rotation("API_SERVER_KEY", "newsecret", working_directory=str(workdir))
    # token is bound to canonical content, not just new_value
    assert plan.plan_token != rp._content_proof(
        key="API_SERVER_KEY",
        new_value="newsecret",
        changed_paths=[],
        source_digests=[],
        active_runs=0,
        requires_restart=True,
        nonce="x",
        created_at=0.0,
    )
    # manual plan with correct new_value but no token fails
    manual = RotationPlan(
        key="API_SERVER_KEY",
        new_value="newsecret",
        changed_paths=plan.changed_paths,
        backup_paths=plan.backup_paths,
        plan_token="",
        nonce="",
        operation_id=plan.operation_id,
        dry_run=False,
    )
    with pytest.raises(ValueError):
        apply_rotation(manual)


def test_apply_rejects_dry_run(tmp_path, monkeypatch):
    plan = RotationPlan(key="API_SERVER_KEY", new_value="x", dry_run=True)
    with pytest.raises(RuntimeError):
        apply_rotation(plan)


def test_apply_writes_atomically_and_unique_backup(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    env = workdir / ".env"
    env.write_text("API_SERVER_KEY=old\n")
    fu = _fake_unit(env={"API_SERVER_KEY": "old"}, working_dir=str(workdir))
    monkeypatch.setattr(sr, "_systemd_unit_environment", fu)
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "hermes-gateway.service")
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 0)
    plan = plan_rotation("API_SERVER_KEY", "newsecret", working_directory=str(workdir))
    plan = RotationPlan(**{**plan.__dict__, "dry_run": False})
    res = apply_rotation(plan)
    assert res.changed_paths == plan.changed_paths
    content = env.read_text()
    assert "API_SERVER_KEY=newsecret" in content
    # backup is unique, not .pre-rotation fixed
    assert any(".pre-rotation-" in b for b in res.backup_paths)
    assert not any(b.endswith(".pre-rotation") for b in res.backup_paths)
    # token consumed: replay fails
    with pytest.raises(ValueError):
        apply_rotation(RotationPlan(**{**plan.__dict__, "dry_run": False}))


def test_apply_rollback_on_second_path_failure(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    env1 = workdir / "a.env"
    env2 = workdir / "b.env"
    env1.write_text("API_SERVER_KEY=old\n")
    env2.write_text("API_SERVER_KEY=old\n")
    # force two changed paths
    monkeypatch.setattr(
        sr, "discover_api_server_key", lambda working_directory=None: type(
            "R", (), {"sources": [
                {"source": "working_dir_env", "path": str(env1), "present": True},
                {"source": "working_dir_env2", "path": str(env2), "present": True},
            ], "status": "consistent"}
        )()
    )
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 0)
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "hermes-gateway.service")

    plan = RotationPlan(
        key="API_SERVER_KEY",
        new_value="newsecret",
        changed_paths=[str(env1), str(env2)],
        backup_paths=[f"{env1}.pre-rotation-x", f"{env2}.pre-rotation-x"],
        source_digests=["d1", "d2"],
        plan_token="tok",
        nonce="n",
        operation_id="x",
        dry_run=False,
    )
    monkeypatch.setattr(rp, "verify_and_consume", lambda **k: None)
    # make second atomic write fail
    real_write = sr._atomic_write_text

    def _bad(path, content):
        if path == str(env2):
            raise RuntimeError("simulated failure on second path")
        return real_write(path, content)

    monkeypatch.setattr(sr, "_atomic_write_text", _bad)
    with pytest.raises(RuntimeError):
        apply_rotation(plan)
    # env1 reverted to original
    assert env1.read_text() == "API_SERVER_KEY=old\n"
    assert env2.read_text() == "API_SERVER_KEY=old\n"


def test_rollback_rotation_uses_manifest(tmp_path):
    target = tmp_path / "a.env"
    target.write_text("API_SERVER_KEY=rotated\n")
    backup = tmp_path / "a.env.pre-rotation-1"
    backup.write_text("API_SERVER_KEY=original\n")
    res = rollback_rotation({
        "changed_paths": [str(target)],
        "backup_paths": [str(backup)],
        "operation_id": "1",
    })
    assert res["status"] == "rolled_back"
    assert target.read_text() == "API_SERVER_KEY=original\n"


def test_rollback_rejects_symlink(tmp_path):
    target = tmp_path / "a.env"
    target.write_text("API_SERVER_KEY=rotated\n")
    backup = tmp_path / "b.env"
    backup.write_text("API_SERVER_KEY=original\n")
    link = tmp_path / "link.env"
    os.symlink(target, str(link))
    with pytest.raises(ValueError):
        rollback_rotation({
            "changed_paths": [str(link)],
            "backup_paths": [str(backup)],
            "operation_id": "1",
        })


def test_schedule_external_restart_separates_units(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_BRIDGE_TEMP_DIR", str(tmp_path / "restart"))
    captured = {}

    def _fake_check_output(argv, text=True):
        captured["argv"] = list(argv)
        return "scheduled"

    monkeypatch.setattr(sr.subprocess, "check_output", _fake_check_output)
    res = schedule_external_restart("hermes-gateway.service")
    assert res["target_service"] == "hermes-gateway.service"
    assert res["transient_unit"] != "hermes-gateway.service"
    assert len(res["transient_unit"]) <= 55
    # no --timer-property
    assert not any(a.startswith("--timer-property") for a in captured["argv"])
    # script invokes real service, not truncated
    script = Path(res["script_path"]).read_text()
    assert shlex.quote("hermes-gateway.service") in script
    assert "trap" in script
    assert "systemctl --user restart" in script


def test_schedule_external_restart_unit_name_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_BRIDGE_TEMP_DIR", str(tmp_path / "restart"))
    long_name = "hermes-gateway-very-long-unit-name-that-exceeds-limits.service"
    res = schedule_external_restart(long_name)
    assert len(res["transient_unit"]) <= 55
    assert res["target_service"] == long_name
    assert "systemctl --user restart" in Path(res["script_path"]).read_text()


def test_no_secrets_in_reports(tmp_path):
    rep = discover_api_server_key(working_directory=None)
    text = str(rep.__dict__)
    # only digests/lengths, never raw value
    assert "old" not in text
    assert "secret" not in text
