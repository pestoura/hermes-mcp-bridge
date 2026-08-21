from pathlib import Path

RUNNER = Path(__file__).parents[1] / "src" / "hermes_mcp_bridge" / "http_runner.py"


def test_factory_northbound_is_composed_before_http_app_and_reinstrumented() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "from .factory_northbound import configure_factory_northbound" in source
    assert "instrument_all_tools" in source
    assert "server_tool_names" in source

    compose = "configure_factory_northbound(mcp, settings)"
    reinstrument = "instrument_all_tools(mcp)"
    app = "app = mcp.streamable_http_app()"

    assert compose in source
    assert reinstrument in source
    assert app in source
    assert source.index(compose) < source.index(reinstrument) < source.index(app)

    # The startup count must describe the effective surface after optional
    # Factory registration, not the baseline count captured by server import.
    assert "instrumented_tools=len(server_tool_names())" in source
