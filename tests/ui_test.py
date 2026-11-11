"""Streamlit UI tests with streamlit.testing.AppTest (headless): every page renders, and the key flows work
through the actual widgets: provider configuration, MCP server management, review and generation.
"""

from __future__ import annotations

import sys

import pytest
from streamlit.testing.v1 import AppTest

from models.agent_models import TaskType
from services.workflow_service import WorkflowService
from testkit import scripted_outputs as so
from testkit.helpers import ScriptedModel
from tests.conftest import ROOT, load_sources, load_template

PAGES = [
    "Dashboard",
    "AI Provider Configuration",
    "MCP Tool Configuration",
    "Tool Registry",
    "Document Template Upload",
    "Source Documents",
    "AI Instructions",
    "Agent Workflow",
    "Document Review",
    "Document Generation",
    "History and Downloads",
    "Application Settings",
]


@pytest.fixture()
def app(data_dir):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90)
    at.run()
    assert not at.exception
    container = at.session_state["_docsgen_container"]
    container.settings.stdio_command_allowlist.append(sys.executable.rsplit("/", 1)[-1])
    yield at, container
    container.shutdown()


def goto(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def button(at, label):
    matches = [b for b in at.button if b.label == label]
    assert matches, f"button {label!r} not found; have {[b.label for b in at.button]}"
    return matches[0]


def test_all_pages_render_empty_and_populated(app):
    at, c = app
    for page in PAGES:
        goto(at, page)
    load_template(c)
    load_sources(c)
    for page in PAGES:
        goto(at, page)
        assert not at.error, (page, [e.value for e in at.error])


def test_ai_provider_configuration_flow(app):
    at, c = app
    c.llm._factory = lambda cfg, secrets: ScriptedModel({}, text_reply="OK")
    goto(at, "AI Provider Configuration")
    at.selectbox(key="ai_provider").set_value(
        __import__(
            "models.provider_models", fromlist=["ProviderId"]
        ).ProviderId.ANTHROPIC
    ).run()
    at.text_input(key="ai_anthropic_secret_api_key").input(
        "sk-ant-ui-test-1234567890"
    ).run()
    at.text_input(key="ai_anthropic_model_custom").input("claude-ui-test").run()
    button(at, "Test connection").click().run()
    assert any("Connected" in s.value for s in at.success), [
        s.value for s in at.success
    ]
    button(at, "Apply configuration").click().run()
    assert c.llm.describe() == "Anthropic Claude · claude-ui-test"
    assert (
        c.credentials.get_secret("llm/anthropic", "api_key")
        == "sk-ant-ui-test-1234567890"
    )
    assert c.credentials.secret_source("llm/anthropic", "api_key") == "session"
    # the secret never appears in rendered output
    rendered = " ".join(str(getattr(e, "value", "")) for e in at.main)
    assert "sk-ant-ui-test-1234567890" not in rendered
    button(at, "Clear credentials").click().run()
    assert c.credentials.get_secret("llm/anthropic", "api_key") is None


def test_mcp_server_add_connect_and_tool_toggle(app):
    at, c = app
    goto(at, "MCP Tool Configuration")
    button(at, "➕ Add MCP server").click().run()
    at.text_input(key="mcpf_new_name").input("Demo Server").run()
    at.text_input(key="mcpf_new_id").input("demo").run()
    at.text_input(key="mcpf_new_cmd").input(sys.executable).run()
    at.text_area(key="mcpf_new_args").input(
        str(ROOT / "mcp_servers" / "demo_server.py")
    ).run()
    button(at, "Save & connect").click().run()
    assert not at.exception
    assert c.mcp.state("demo").status == "connected"
    assert any(
        "connected: 4 tool(s)" in s.value or "Saved and connected" in s.value
        for s in at.success
    )
    assert c.mcp.is_persisted("demo")
    # Disable all tools through the UI
    button(at, "Disable all (filtered)").click().run()
    assert c.mcp.available_tools() == {}
    button(at, "Disconnect").click().run()
    assert c.mcp.state("demo").status == "disconnected"


def test_review_and_generation_flow(app):
    at, c = app
    load_template(c)
    load_sources(c)
    c.llm._factory = lambda cfg, secrets: ScriptedModel(so.OUTPUTS)
    c.credentials.set_secret(
        "llm/openai", "api_key", "sk-ui-0000000000000", persist=False
    )
    from models.provider_models import ProviderConfig, ProviderId

    c.llm.apply(ProviderConfig(provider=ProviderId.OPENAI, model_id="scripted"))
    state = WorkflowService.new_state(TaskType.GENERATE_COMPLETE, "Fill it.")
    c.session.workflow = state
    c.workflow.run_pipeline(state, c.document_context())

    goto(at, "Document Review")
    assert any("Missing information" in e.label for e in at.expander)
    button(at, "✅ Approve all pending").click().run()
    assert state.approved_drafts
    goto(at, "Document Generation")
    button(at, "🏗️ Generate final document").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(c.session.history) == 1
    assert any("Generated" in s.value for s in at.success)
    goto(at, "History and Downloads")
    assert not at.error


def test_workflow_page_runs_pipeline(app):
    at, c = app
    load_template(c)
    load_sources(c, names=("sample_source.txt",))
    c.llm._factory = lambda cfg, secrets: ScriptedModel(so.OUTPUTS)
    c.credentials.set_secret(
        "llm/openai", "api_key", "sk-ui-0000000000000", persist=False
    )
    from models.provider_models import ProviderConfig, ProviderId

    c.llm.apply(ProviderConfig(provider=ProviderId.OPENAI, model_id="scripted"))
    goto(at, "Agent Workflow")
    button(at, "▶ Run complete workflow").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert c.session.workflow is not None and c.session.workflow.drafts
