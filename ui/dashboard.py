"""Dashboard: overview, status of AI and MCP connections, documents, workflow and recent history."""

from __future__ import annotations

import streamlit as st

from config import APP_NAME, APP_VERSION
from models.agent_models import TASK_LABELS, TaskType
from services.container import ServiceContainer
from ui.components import badge, goto_button, show_flashes


def render(c: ServiceContainer) -> None:
    st.markdown(
        f"""<div class="dg-hero"><h1>📄 {APP_NAME}</h1>
        <p>Intelligent document automation: Strands agents read your sources and fill
        your own Word template — formatting intact, every fact traceable, nothing written
        without your approval.</p></div>""",
        unsafe_allow_html=True,
    )
    show_flashes()
    for message in c.startup_messages:
        st.warning(message)

    status = c.llm.status
    llm_label = (
        "Not configured"
        if not c.llm.is_configured()
        else (
            "Connected"
            if status.ok
            else (
                "Connection error" if status.ok is False else "Configured (not tested)"
            )
        )
    )
    llm_kind = (
        "disabled"
        if not c.llm.is_configured()
        else ("ok" if status.ok else ("error" if status.ok is False else "pending"))
    )
    servers = c.mcp.list_servers()
    connected = [s for s in servers if c.mcp.state(s.id).status == "connected"]
    errored = [s for s in servers if c.mcp.state(s.id).status == "error"]
    descriptors = c.tools.descriptors()
    enabled_tools = [d for d in descriptors if d.enabled]
    state = c.session.workflow

    row1 = st.columns(4)
    with row1[0].container(border=True):
        st.caption("AI provider")
        st.markdown(f"**{c.llm.describe()}**")
        st.markdown(badge(llm_label, llm_kind))
    with row1[1].container(border=True):
        st.caption("MCP servers")
        st.markdown(f"**{len(connected)} / {len(servers)} connected**")
        st.markdown(
            badge(f"{len(errored)} with errors", "error")
            if errored
            else badge(
                "No errors" if servers else "None configured",
                "ok" if servers else "disabled",
            )
        )
    with row1[2].container(border=True):
        st.caption("Available tools")
        st.markdown(f"**{len(enabled_tools)} enabled**")
        st.caption(
            f"{sum(1 for d in enabled_tools if d.source == 'builtin')} built-in ·"
            f" {sum(1 for d in enabled_tools if d.source == 'mcp')} MCP"
        )
    with row1[3].container(border=True):
        st.caption("Documents")
        st.markdown(
            f"**Template:** {'✓' if c.session.template else '—'} · **Sources:**"
            f" {len(c.session.sources)}"
        )
        st.caption(
            c.session.template.file_name
            if c.session.template
            else "No template uploaded"
        )

    row2 = st.columns([2, 3])
    with row2[0].container(border=True):
        st.markdown("**Current workflow**")
        if state is None:
            st.markdown(badge("Not started", "disabled"))
        else:
            st.markdown(
                f"{TASK_LABELS[state.task_type]} "
                f" \n{badge(state.stage.value.replace('_', ' '), 'error' if state.stage.value == 'failed' else 'info')}"
            )
            st.caption(
                f"Run {state.run_id} · {len(state.approved_drafts)}/{len(state.drafts)}"
                " approved"
            )
            if state.last_error:
                st.error(state.last_error)
    with row2[1].container(border=True):
        st.markdown("**Recent document generation**")
        if not c.session.history:
            st.caption("No documents generated yet.")
        else:
            st.dataframe(
                [
                    {
                        "Version": r.version,
                        "File": r.file_name,
                        "Created": r.created_at.strftime("%H:%M:%S"),
                        "Validation": r.validation_summary,
                    }
                    for r in reversed(c.session.history[-5:])
                ],
                hide_index=True,
                width="stretch",
            )

    st.markdown("#### Getting started")
    steps = [
        (
            c.llm.is_configured() and status.ok is not False,
            "Configure and test an AI provider",
            "AI Provider Configuration",
        ),
        (
            bool(connected) or not servers,
            "Optional: connect MCP servers and choose tools",
            "MCP Tool Configuration",
        ),
        (
            any(d.source == "mcp" for d in descriptors)
            and any(c.tools.permissions().values()),
            "Assign tools to agents",
            "Tool Registry",
        ),
        (
            c.session.template is not None,
            "Upload your DOCX template",
            "Document Template Upload",
        ),
        (bool(c.session.sources), "Upload supporting documents", "Source Documents"),
        (bool(c.session.instructions), "Write instructions", "AI Instructions"),
        (
            state is not None and bool(state.drafts),
            "Run the agent workflow",
            "Agent Workflow",
        ),
        (
            state is not None and bool(state.approved_drafts),
            "Review and approve content",
            "Document Review",
        ),
        (
            bool(c.session.history),
            "Generate and download the document",
            "Document Generation",
        ),
    ]
    for i, (done, label, page) in enumerate(steps):
        cols = st.columns([0.4, 5, 1.6])
        cols[0].markdown("✅" if done else f"**{i + 1}**")
        cols[1].markdown(label)
        with cols[2]:
            goto_button("Open", page, key=f"dash_go_{i}", width="stretch")
    st.caption(
        f"{APP_NAME} {APP_VERSION} · Tasks available: "
        + ", ".join(TASK_LABELS[t] for t in TaskType)
    )
