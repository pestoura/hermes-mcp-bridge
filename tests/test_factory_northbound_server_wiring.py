from pathlib import Path


SERVER = Path(__file__).parents[1] / "src" / "hermes_mcp_bridge" / "server.py"


def test_factory_northbound_is_composed_before_tool_instrumentation() -> None:
    source = SERVER.read_text(encoding="utf-8")

    assert "from .factory_northbound import configure_factory_northbound" in source
    compose = "FACTORY_NORTHBOUND_TOOLS = configure_factory_northbound(mcp, settings)"
    instrument = "INSTRUMENTED_TOOL_COUNT = instrument_all_tools(mcp)"
    assert compose in source
    assert source.index(compose) < source.index(instrument)
