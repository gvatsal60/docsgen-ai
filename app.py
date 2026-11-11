"""DocsGen — Streamlit entry point.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

from config import APP_NAME, APP_VERSION
from storage.session_store import get_container
from ui import (
    ai_configuration,
    dashboard,
    download,
    instructions,
    mcp_configuration,
    review,
    settings,
    theme,
    tool_registry,
    upload,
    workflow,
)
from ui.components import NAV_KEY, apply_pending_navigation, badge

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


st.set_page_config(
    page_title=APP_NAME, page_icon="📄", layout="wide", initial_sidebar_state="expanded"
)


PAGES = {
    "Dashboard": ("🏠", dashboard.render),
    "AI Provider Configuration": ("🧠", ai_configuration.render),
    "MCP Tool Configuration": ("🔌", mcp_configuration.render),
    "Tool Registry": ("🧰", tool_registry.render),
    "Document Template Upload": ("📄", upload.render_template),
    "Source Documents": ("📚", upload.render_sources),
    "AI Instructions": ("✍️", instructions.render),
    "Agent Workflow": ("🤖", workflow.render),
    "Document Review": ("🔍", review.render),
    "Document Generation": ("🏗️", download.render_generation),
    "History and Downloads": ("🗂️", download.render_history),
    "Application Settings": ("⚙️", settings.render),
}


def main() -> None:
    theme.inject_css()
    container = get_container()
    apply_pending_navigation()
    if st.session_state.get(NAV_KEY) not in PAGES:
        st.session_state[NAV_KEY] = "Dashboard"

    with st.sidebar:
        st.markdown(f"### 📄 {APP_NAME}")
        st.caption("AI document automation")
        page = st.radio(
            "Navigation",
            list(PAGES),
            key=NAV_KEY,
            format_func=lambda p: f"{PAGES[p][0]}  {p}",
            label_visibility="collapsed",
        )
        st.divider()
        status = container.llm.status
        if not container.llm.is_configured():
            st.markdown(badge("LLM not configured", "disabled"))
        else:
            kind = "ok" if status.ok else ("error" if status.ok is False else "pending")
            st.markdown(
                badge(
                    "LLM "
                    + (
                        "connected"
                        if status.ok
                        else ("error" if status.ok is False else "untested")
                    ),
                    kind,
                )
            )
            st.caption(container.llm.describe())
        servers = container.mcp.list_servers()
        connected = sum(
            1 for s in servers if container.mcp.state(s.id).status == "connected"
        )
        st.markdown(
            badge(
                f"MCP {connected}/{len(servers)} connected",
                "ok" if connected else "disabled",
            )
        )
        wf = container.session.workflow
        if wf is not None:
            st.caption(f"Workflow: {wf.stage.value.replace('_', ' ')}")
        st.caption(f"v{APP_VERSION}")

    _, render = PAGES[page]
    render(container)


main()
