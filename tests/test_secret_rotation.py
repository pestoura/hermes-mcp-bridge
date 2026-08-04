import os
from pathlib import Path

import pytest

from hermes_mcp_bridge import secret_rotation as sr


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setattr(
        "hermes_mcp_bridge.secret_rotation.get_settings",
        lambda: type("S", (), {"bridge_version": "0.7.0"})(),
    )


def _fake_read(monkeypatch, mapping):
    def fake(path):
        return dict(mapping.get(path, {}))

    monkeypatch.setattr(sr, "_read_env_file", fake)


def test_discover_single_source_insufficient(settings, monkeypatch):
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: None)
    _fake_read(monkeypatch, {".env": {"API_SERVER_KEY": "value-a"}})
    rep = sr.discover_api_server_key()
    assert rep.status == "insufficient"
    assert rep.comparable is False


def test_discover_mismatch(settings, monkeypatch):
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "unit.service")
    monkeypatch.setattr(
        sr, "_systemd_unit_environment",
        lambda u: ({"API_SERVER_KEY": "value-a"}, [], None),
    )
    _fake_read(monkeypatch, {".env": {"API_SERVER_KEY": "value-b"}})
    rep = sr.discover_api_server_key()
    assert rep.status == "mismatch"
    assert rep.comparable is False


def test_discover_consistent(settings, monkeypatch):
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "unit.service")
    monkeypatch.setattr(
        sr, "_systemd_unit_environment",
        lambda u: ({"API_SERVER_KEY": "value-a"}, [], "/abs/workdir"),
    )
    _fake_read(monkeypatch, {"/abs/workdir/.env": {"API_SERVER_KEY": "value-a"}})
    rep = sr.discover_api_server_key()
    assert rep.status == "consistent"
    assert rep.comparable is True
    wd = next(s for s in rep.sources if s["source"] == "working_dir_env")
    assert wd["path"].startswith("/")


def test_discover_env_vs_container_mismatch(settings, monkeypatch):
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: None)
    _fake_read(
        monkeypatch,
        {
            os.path.abspath(os.path.join("compose", ".env")): {"HERMES_API_KEY": "compose-val"},
            os.path.abspath(".env"): {"HERMES_API_KEY": "container-val"},
        },
    )
    rep = sr.discover_hermes_api_key()
    assert rep.status == "mismatch"
    paths = [s["path"] for s in rep.sources]
    assert os.path.abspath(os.path.join("compose", ".env")) in paths
    assert os.path.abspath(".env") in paths


def test_discover_no_relative_path_read(settings, monkeypatch, tmp_path):
    # relative path (basename) must be rejected; absolute path allowed
    assert sr._read_env_file("relative.env") == {}
    abs_path = os.path.abspath("relative.env")
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write("API_SERVER_KEY=leak\n")
    try:
        assert sr._read_env_file(abs_path) == {"API_SERVER_KEY": "leak"}
    finally:
        os.remove(abs_path)


def test_plan_transports_absolute_paths(settings, monkeypatch):
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "unit.service")
    monkeypatch.setattr(
        sr, "_systemd_unit_environment",
        lambda u: ({"API_SERVER_KEY": "value-a"}, [], "/abs/workdir"),
    )
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 0)
    _fake_read(monkeypatch, {"/abs/workdir/.env": {"API_SERVER_KEY": "value-a"}})
    plan = sr.plan_rotation("API_SERVER_KEY", "newvalue")
    for p in plan.changed_paths + plan.backup_paths:
        assert os.path.isabs(p)
        assert os.path.realpath(p) == p
    assert plan.plan_token


def test_plan_rejects_relative_path(settings, monkeypatch):
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: None)
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 0)
    bad = sr.RotationPlan(
        key="API_SERVER_KEY",
        new_value="newvalue",
        dry_run=False,
        plan_token=sr._plan_token("newvalue"),
        changed_paths=["relative.env"],
        backup_paths=["relative.env.pre-rotation"],
    )
    with pytest.raises(ValueError):
        sr._validate_plan_paths(bad)


def test_plan_aborts_with_active_runs(settings, monkeypatch):
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 3)
    with pytest.raises(RuntimeError):
        sr.plan_rotation("API_SERVER_KEY", "newvalue")


def test_plan_aborts_when_health_unknown(settings, monkeypatch):
    monkeypatch.setattr(sr, "_active_api_runs", lambda: -1)
    with pytest.raises(RuntimeError):
        sr.plan_rotation("API_SERVER_KEY", "newvalue")


def test_apply_rejects_dry_run(settings):
    plan = sr.RotationPlan(key="API_SERVER_KEY", dry_run=True)
    with pytest.raises(RuntimeError):
        sr.apply_rotation(plan)


def test_apply_rejects_bad_token(settings):
    plan = sr.RotationPlan(
        key="API_SERVER_KEY",
        new_value="newvalue",
        dry_run=False,
        plan_token="wrongtoken",
        changed_paths=["/abs/.env"],
        backup_paths=["/abs/.env.pre-rotation"],
    )
    with pytest.raises(ValueError):
        sr.apply_rotation(plan)


def test_apply_revalidates_active_runs(settings, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("API_SERVER_KEY=old\n")
    plan = sr.RotationPlan(
        key="API_SERVER_KEY",
        new_value="newvalue",
        dry_run=False,
        plan_token=sr._plan_token("newvalue"),
        changed_paths=[str(env)],
        backup_paths=[str(env) + ".pre-rotation"],
    )
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 4)
    with pytest.raises(RuntimeError):
        sr.apply_rotation(plan)
    assert "API_SERVER_KEY=old" in env.read_text()


def test_apply_writes_atomic_and_creates_backup(settings, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("API_SERVER_KEY=old\nOTHER=keep\n")
    plan = sr.RotationPlan(
        key="API_SERVER_KEY",
        new_value="newvalue",
        dry_run=False,
        plan_token=sr._plan_token("newvalue"),
        changed_paths=[str(env)],
        backup_paths=[str(env) + ".pre-rotation"],
    )
    monkeypatch.setattr(sr, "_active_api_runs", lambda: 0)
    res = sr.apply_rotation(plan)
    text = env.read_text()
    assert "API_SERVER_KEY=newvalue" in text
    assert "OTHER=keep" in text
    assert os.path.exists(str(env) + ".pre-rotation")
    assert oct(os.stat(str(env)).st_mode & 0o777) == "0o600"
    assert res.dry_run is False


def test_schedule_external_restart_separates_units(settings, monkeypatch):
    captured = {}

    def fake_check_output(argv, text=True):
        captured["argv"] = argv
        return "scheduled"

    monkeypatch.setattr(sr.subprocess, "check_output", fake_check_output)
    result = sr.schedule_external_restart("hermes-gateway.service", timeout_seconds=120)
    assert result["target_service"] == "hermes-gateway.service"
    assert result["transient_unit"] != "hermes-gateway.service"
    assert len(result["transient_unit"]) <= 55
    assert os.path.isabs(result["script_path"])
    assert oct(os.stat(result["script_path"]).st_mode & 0o777) == "0o700"
    script_text = Path(result["script_path"]).read_text()
    assert "hermes-gateway.service" in script_text
    assert "systemctl" in script_text
    assert "--unit=" in " ".join(captured["argv"])


def test_rollback_uses_external_reconcile(settings, tmp_path):
    backup = tmp_path / ".env.pre-rotation"
    backup.write_text("API_SERVER_KEY=old\n")
    plan = sr.RotationPlan(
        key="API_SERVER_KEY",
        changed_paths=[str(tmp_path / ".env")],
        backup_paths=[str(backup)],
        target_service="hermes-gateway.service",
    )
    res = sr.rollback_rotation(plan)
    assert res["status"] == "ok"
    assert str(tmp_path / ".env") in res["restored"]
    assert "next" in res and "externally" in res["next"]


def test_verify_does_not_leak_value(settings, monkeypatch):
    monkeypatch.setattr(sr, "_gateway_unit_name", lambda: "unit.service")
    monkeypatch.setattr(
        sr, "_systemd_unit_environment",
        lambda u: ({"API_SERVER_KEY": "secretvalue"}, [], "/abs/workdir"),
    )
    _fake_read(monkeypatch, {"/abs/workdir/.env": {"API_SERVER_KEY": "secretvalue"}})
    res = sr.verify_rotation("API_SERVER_KEY")
    assert res["status"] == "verified"
    assert "secretvalue" not in str(res)
