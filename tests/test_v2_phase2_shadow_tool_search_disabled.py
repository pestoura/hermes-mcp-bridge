"""Regression: the Phase 2 disposable shadow home must disable tool search.

Hermes' progressive tool disclosure ("tool search") replaces every deferrable
MCP tool in the model-facing tools array with a generic
``tool_search``/``tool_describe``/``tool_call`` bridge. Under that bridge the
shadow agent never emits a *named* GitHub MCP tool call, so provenance recovery
from the disposable shadow state database observes only bridge calls and
fail-closes with ``PROVENANCE_UNAUTHORIZED_TOOL_CALL``.

The acceptance contract requires the five named read-only tools to be directly
callable and individually attributable, so the shadow config must pin
``tools.tool_search.enabled: off``. The runtime isolation probe must also
verify it, so a shadow home prepared without the pin cannot be proven exact.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PREPARE_PATH = ROOT / "scripts" / "v2_phase2_prepare_shadow_home.py"
PROBE_PATH = ROOT / "scripts" / "v2_phase2_probe_shadow_runtime.py"


def _load_prepare_module():
    spec = importlib.util.spec_from_file_location(
        "v2_phase2_prepare_shadow_home_tool_search", PREPARE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREPARE = _load_prepare_module()


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _prepare_args(tmp_path: Path) -> argparse.Namespace:
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
        mcp_python=str(_executable(tmp_path / "venv" / "bin" / "python")),
        mcp_script=str(PREPARE_PATH),
        token_file=str(token),
        repository="owner/repo",
        api_port=8123,
        api_key_out=str(tmp_path / "api.key"),
        hermes_python=None,
    )


@pytest.fixture()
def stub_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PREPARE, "_constrain_platform_to_shadow_mcp", lambda target: 0)


def test_shadow_config_disables_tool_search_bridge(tmp_path: Path, stub_resolver: None) -> None:
    args = _prepare_args(tmp_path)
    result = PREPARE.prepare(args)
    assert result["status"] == "SHADOW_HOME_PREPARED"

    config = yaml.safe_load((Path(args.shadow_home) / "config.yaml").read_text(encoding="utf-8"))
    assert config["tools"]["tool_search"]["enabled"] == "off"


def test_shadow_config_keeps_named_mcp_tool_include_list(
    tmp_path: Path, stub_resolver: None
) -> None:
    """Disabling the bridge must not widen or narrow the named tool surface."""
    args = _prepare_args(tmp_path)
    PREPARE.prepare(args)
    config = yaml.safe_load((Path(args.shadow_home) / "config.yaml").read_text(encoding="utf-8"))
    server = config["mcp_servers"][PREPARE.SHADOW_MCP_SERVER]
    assert server["tools"]["include"] == list(PREPARE.SHADOW_MCP_TOOL_NAMES)
    assert server["tools"]["resources"] is False
    assert server["tools"]["prompts"] is False


def test_probe_child_requires_tool_search_off() -> None:
    """The runtime isolation probe must assert the pin, not just the MCP block."""
    source = PROBE_PATH.read_text(encoding="utf-8")
    assert '"tool_search"' in source
    assert '== "off"' in source
