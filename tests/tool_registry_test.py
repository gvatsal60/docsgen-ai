"""Central tool registry: built-in tools, MCP tools, enable/disable, agent permissions, guard hook."""

from __future__ import annotations

import json

from models.agent_models import AgentRole
from tests.conftest import load_sources, load_template
from tools.registry import BUILTIN_TOOLS


def test_builtin_tools_registered_with_default_permissions(container):
    names = {d.name for d in container.tools.descriptors() if d.source ==
             "builtin"}
    assert names == {d.name for d in BUILTIN_TOOLS}
    perms = container.tools.permissions()
    assert "get_template_outline" in perms[AgentRole.DOCUMENT_ANALYZER]
    assert "read_source_document" in perms[AgentRole.INFORMATION_EXTRACTOR]
    assert "read_source_document" not in perms[AgentRole.QUALITY_REVIEWER]


def test_builtin_enable_disable_affects_agents(container):
    ctx = container.document_context()
    before = [
        d.name
        for d in container.agents.agent_tools(AgentRole.INFORMATION_EXTRACTOR, ctx)
    ]
    container.tools.set_builtin_enabled(["read_source_document"], False)
    after = [
        d.name
        for d in container.agents.agent_tools(AgentRole.INFORMATION_EXTRACTOR, ctx)
    ]
    assert "read_source_document" in before and "read_source_document" not in after


def test_mcp_tool_needs_connection_enable_and_authorization(
    container, demo_stdio_config
):
    ctx = container.document_context()
    role = AgentRole.RESEARCH_AGENT
    container.mcp.save_server(demo_stdio_config, persist=False, is_new=True)
    container.tools.set_permission(role, "demo_lookup_company", True)
    assert "demo_lookup_company" not in [
        d.name for d in container.agents.agent_tools(role, ctx)
    ]  # 1. not connected
    container.mcp.connect("demo")
    assert "demo_lookup_company" in [
        d.name for d in container.agents.agent_tools(role, ctx)
    ]
    container.mcp.set_tools_enabled(
        "demo", ["lookup_company"], False)  # 2. disabled
    assert "demo_lookup_company" not in [
        d.name for d in container.agents.agent_tools(role, ctx)
    ]
    container.mcp.set_tools_enabled("demo", ["lookup_company"], True)
    container.tools.set_permission(
        role, "demo_lookup_company", False
    )  # 3. not authorized
    assert "demo_lookup_company" not in [
        d.name for d in container.agents.agent_tools(role, ctx)
    ]
    # other agents never get MCP tools unless authorized
    for other in AgentRole:
        assert not any(
            d.source == "mcp" for d in container.agents.agent_tools(other, ctx)
        )


def test_filters_and_reset(container, demo_stdio_config):
    container.mcp.save_server(demo_stdio_config, persist=False, is_new=True)
    container.mcp.connect("demo")
    desc = container.tools.descriptors()
    assert {d.name for d in container.tools.filter(desc, source="mcp")} == {
        "demo_lookup_company",
        "demo_word_count",
        "demo_current_date",
        "demo_delete_record",
    }
    assert [d.name for d in container.tools.filter(desc, query="readability")] == [
        "readability_stats"
    ]
    assert all(
        d.server_id == "demo" for d in container.tools.filter(desc, server_id="demo")
    )
    container.tools.set_permission(
        AgentRole.QUALITY_REVIEWER, "demo_word_count", True)
    container.tools.reset_permissions()
    assert (
        "demo_word_count"
        not in container.tools.permissions()[AgentRole.QUALITY_REVIEWER]
    )


def test_builtin_tool_execution(container):
    load_template(container)
    load_sources(container, names=("sample_source.txt", "sample_budget.csv"))
    ctx = container.document_context()
    outline = container.tools.execute_builtin("get_template_outline", {}, ctx)
    assert "[s01] 1. Executive Summary" in outline and "<untrusted_data" in outline
    hits = container.tools.execute_builtin(
        "search_source_documents", {"query": "budget"}, ctx
    )
    assert "D1-C" in hits or "D2-C" in hits
    stats = json.loads(
        container.tools.execute_builtin(
            "csv_statistics", {"doc_id": "D2"}, ctx)
    )
    assert stats["Cost (USD)"]["sum"] == 248000
    check = json.loads(
        container.tools.execute_builtin(
            "verify_values_in_sources",
            {"text": "Budget USD 248,000 and USD 999,999"},
            ctx,
        )
    )
    assert "USD 248,000" in check["supported"] and "USD 999,999" in check["unsupported"]
    assert container.tools.executions[0].status == "success"
