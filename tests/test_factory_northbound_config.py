import pytest

from hermes_mcp_bridge.config import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_API_KEY", "test-key")
    monkeypatch.delenv("HERMES_API_KEY_FILE", raising=False)
    monkeypatch.delenv("HERMES_FACTORY_NORTHBOUND_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_FACTORY_REGISTRY_PATH", raising=False)


def test_factory_northbound_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    settings = Settings(_env_file=None)

    assert settings.hermes_factory_northbound_enabled is False
    assert settings.hermes_factory_registry_path == ""


def test_explicit_factory_enable_requires_registry_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("HERMES_FACTORY_NORTHBOUND_ENABLED", "true")

    with pytest.raises(ValueError, match="HERMES_FACTORY_REGISTRY_PATH"):
        Settings(_env_file=None)


def test_factory_enable_accepts_explicit_registry_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("HERMES_FACTORY_NORTHBOUND_ENABLED", "true")
    monkeypatch.setenv("HERMES_FACTORY_REGISTRY_PATH", "/srv/hermes-factory/state.sqlite3")

    settings = Settings(_env_file=None)

    assert settings.hermes_factory_northbound_enabled is True
    assert settings.hermes_factory_registry_path == "/srv/hermes-factory/state.sqlite3"
