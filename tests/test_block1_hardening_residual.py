"""Block 1 residual hardening: bounded plan registry and canonical lock path."""

from __future__ import annotations

import os
import time

import pytest

from hermes_mcp_bridge import _rotation_plans as rp
from hermes_mcp_bridge._file_lock import FileLockError, exclusive_file_lock


@pytest.fixture(autouse=True)
def _clean_registry():
    rp._registry.clear()
    yield
    rp._registry.clear()


def _register(index: int) -> tuple[str, str]:
    return rp.register_plan(
        key=f"KEY_{index}",
        new_value="value",
        changed_paths=["/tmp/a"],
        source_digests=["deadbeef"],
        active_runs=0,
        requires_restart=False,
    )


def test_registry_is_bounded() -> None:
    for i in range(rp.MAX_LIVE_PLANS * 3):
        _register(i)
    assert rp.registry_size() <= rp.MAX_LIVE_PLANS


def test_expired_plans_are_purged_on_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    token, _ = _register(0)
    assert token in rp._registry
    monkeypatch.setattr(rp, "PLAN_TTL_SECONDS", 0)
    rp._registry[token].expires_at = time.monotonic() - 1
    _register(1)
    assert token not in rp._registry


def test_purge_expired_returns_count() -> None:
    token, _ = _register(0)
    rp._registry[token].expires_at = time.monotonic() - 1
    assert rp.purge_expired() >= 1
    assert rp.registry_size() == 0


def test_consumed_plan_still_reports_already_consumed() -> None:
    token, nonce = _register(0)
    kwargs = dict(
        key="KEY_0",
        new_value="value",
        changed_paths=["/tmp/a"],
        source_digests=["deadbeef"],
        active_runs=0,
        requires_restart=False,
        nonce=nonce,
    )
    rp.verify_and_consume(plan_token=token, **kwargs)
    with pytest.raises(ValueError, match="already consumed"):
        rp.verify_and_consume(plan_token=token, **kwargs)


def test_consumed_plans_are_evicted_first_under_pressure() -> None:
    token, nonce = _register(0)
    rp.verify_and_consume(
        plan_token=token,
        key="KEY_0",
        new_value="value",
        changed_paths=["/tmp/a"],
        source_digests=["deadbeef"],
        active_runs=0,
        requires_restart=False,
        nonce=nonce,
    )
    for i in range(1, rp.MAX_LIVE_PLANS + 5):
        _register(i)
    assert token not in rp._registry
    assert rp.registry_size() <= rp.MAX_LIVE_PLANS


def test_lock_path_is_canonicalized_via_symlink(tmp_path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    os.symlink(real_dir, link_dir)
    lock_via_link = str(link_dir / "rotation.lock")
    lock_direct = str(real_dir / "rotation.lock")
    with (
        exclusive_file_lock(lock_via_link),
        pytest.raises(FileLockError),
        exclusive_file_lock(lock_direct),
    ):
        pass
    assert (real_dir / "rotation.lock").exists()


def test_lock_file_mode_is_private(tmp_path) -> None:
    lock_path = str(tmp_path / "nested" / "rotation.lock")
    with exclusive_file_lock(lock_path):
        mode = os.stat(lock_path).st_mode & 0o777
        parent_mode = os.stat(os.path.dirname(lock_path)).st_mode & 0o777
    assert mode == 0o600
    assert parent_mode == 0o700


def test_lock_released_after_context(tmp_path) -> None:
    lock_path = str(tmp_path / "rotation.lock")
    with exclusive_file_lock(lock_path):
        pass
    with exclusive_file_lock(lock_path):
        pass  # re-acquirable, no orphan lock


def test_hmac_secret_does_not_enable_cross_process_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_SECRET", "shared-secret")
    token, nonce = _register(0)
    rp._registry.clear()  # simulate another process: empty in-memory registry
    with pytest.raises(ValueError, match="unknown"):
        rp.verify_and_consume(
            plan_token=token,
            key="KEY_0",
            new_value="value",
            changed_paths=["/tmp/a"],
            source_digests=["deadbeef"],
            active_runs=0,
            requires_restart=False,
            nonce=nonce,
        )
