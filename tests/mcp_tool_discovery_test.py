"""Tool classification and description sanitization (untrusted metadata)."""

from __future__ import annotations

from services.mcp_tool_discovery import classify, clean_description


def test_classification_from_annotations_and_names():
    assert classify("delete_file", {"destructiveHint": True})[3] is True
    assert classify("search", {"readOnlyHint": True})[3] is False
    assert classify("send_email", {})[3] is True  # consequential name
    assert classify("web_search", {})[3] is False
    assert classify("query_database", {"readOnlyHint": False})[3] is True
    assert (
        classify("delete_cache", {"readOnlyHint": True})[3] is False
    )  # explicit read-only hint wins over name


def test_description_is_limited_and_cleaned():
    text = "Useful tool\x00\x07" + "x" * 5000
    cleaned = clean_description(text)
    assert "\x00" not in cleaned and len(cleaned) <= 1000


def test_discovery_uses_execution_timeout(container, demo_stdio_config):
    container.mcp.save_server(
        demo_stdio_config.model_copy(update={"tool_timeout": 17}),
        persist=False,
        is_new=True,
    )
    container.mcp.connect("demo")
    tool, _info = next(iter(container.mcp.connections.tools("demo")))
    assert tool.timeout.total_seconds() == 17
    assert len(tool.tool_spec["description"]) <= 1000
