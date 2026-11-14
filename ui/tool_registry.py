"""Tool Registry page: all built-in and MCP tools, enable/disable, agent permissions, execution log."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from models.agent_models import AGENT_LABELS, AgentRole
from services.container import ServiceContainer
from services.security import sanitize_error
from tools.registry import BUILTIN_BY_NAME
from ui.components import flash, page_header, show_flashes


def render(c: ServiceContainer) -> None:
    page_header(
        "Tool Registry",
        "Built-in document tools and MCP tools in one place. Decide which tools each"
        " agent may use.",
        "🧰",
    )
    show_flashes()
    descriptors = c.tools.descriptors()
    builtin = [d for d in descriptors if d.source == "builtin"]
    mcp = [d for d in descriptors if d.source == "mcp"]
    m = st.columns(4)
    m[0].metric("Built-in tools", len(builtin))
    m[1].metric("MCP tools (connected)", len(mcp))
    m[2].metric("Enabled", sum(1 for d in descriptors if d.enabled))
    m[3].metric("Executions logged", len(c.tools.executions))

    tab_tools, tab_perms, tab_log, tab_test = st.tabs(
        ["All tools", "Agent permissions", "Execution log", "Test built-in tool"]
    )

    with tab_tools:
        f = st.columns([2, 1, 2])
        query = f[0].text_input(
            "Search", key="reg_search", placeholder="tool name or description"
        )
        source = f[1].selectbox(
            "Type",
            ["all", "builtin", "mcp"],
            key="reg_source",
            format_func=lambda s: {"all": "All", "builtin": "Built-in", "mcp": "MCP"}[
                s
            ],
        )
        servers = sorted({d.server_id for d in mcp if d.server_id})
        server = f[2].selectbox("MCP server", ["Any"] + servers, key="reg_server")
        filtered = c.tools.filter(
            descriptors,
            query=query,
            source=source,
            server_id=None if server == "Any" else server,
        )
        rows = [
            {
                "Enabled": d.enabled,
                "Tool": d.name,
                "Type": "Built-in" if d.source == "builtin" else "MCP",
                "Category": d.category,
                "Needs approval": "yes" if d.requires_approval else "",
                "Description": d.description[:160],
            }
            for d in filtered
        ]
        edited = st.data_editor(
            rows,
            key="reg_editor",
            hide_index=True,
            width="stretch",
            disabled=["Tool", "Type", "Category", "Needs approval", "Description"],
            column_config={"Enabled": st.column_config.CheckboxColumn(width="small")},
        )
        changed = [
            (d, bool(r["Enabled"]))
            for d, r in zip(filtered, edited)
            if bool(r["Enabled"]) != d.enabled
        ]
        if changed:
            for d, enabled in changed:
                if d.source == "builtin":
                    c.tools.set_builtin_enabled([d.name], enabled)
                elif d.server_id and d.original_name:
                    c.mcp.set_tools_enabled(d.server_id, [d.original_name], enabled)
            st.session_state.pop("reg_editor", None)
            st.rerun()

    with tab_perms:
        st.caption(
            "A tool is available to an agent only when it is enabled, its MCP server is"
            " connected, and the agent is authorized here. MCP tools are never"
            " authorized by default."
        )
        perms = c.tools.permissions()
        roles = list(AgentRole)
        all_names = [d.name for d in descriptors]
        # keep permissions for MCP tools whose server is currently disconnected visible
        offline = sorted(
            {n for names in perms.values() for n in names} - set(all_names)
        )
        matrix = []
        for name in all_names + offline:
            row: dict[str, Any] = {
                "Tool": name + ("  (offline)" if name in offline else "")
            }
            for role in roles:
                row[AGENT_LABELS[role]] = name in perms[role]
            matrix.append(row)
        edited = st.data_editor(
            matrix,
            key="perm_editor",
            hide_index=True,
            width="stretch",
            disabled=["Tool"],
            column_config={
                AGENT_LABELS[r]: st.column_config.CheckboxColumn(width="small")
                for r in roles
            },
        )
        b = st.columns([1, 1, 4])
        if b[0].button("Save permissions", type="primary", width="stretch"):
            new: dict[AgentRole, set[str]] = {role: set() for role in roles}
            for row in edited:
                name = row["Tool"].replace("  (offline)", "")
                for role in roles:
                    if row[AGENT_LABELS[role]]:
                        new[role].add(name)
            c.tools.save_permissions(new)
            st.session_state.pop("perm_editor", None)
            flash("Agent permissions saved.")
            st.rerun()
        if b[1].button("Reset to defaults", width="stretch"):
            c.tools.reset_permissions()
            st.session_state.pop("perm_editor", None)
            flash(
                "Permissions reset: built-in defaults restored, MCP tools unassigned.",
                "info",
            )
            st.rerun()

        st.markdown("##### Tools each agent can use right now")
        ctx = c.document_context()
        cols = st.columns(3)
        for i, role in enumerate(roles):
            with cols[i % 3].container(border=True):
                names = [d.name for d in c.agents.agent_tools(role, ctx)]
                st.markdown(f"**{AGENT_LABELS[role]}** · {len(names)} tool(s)")
                st.caption(", ".join(names) or "No tools")

    with tab_log:
        if not c.tools.executions:
            st.caption("Tool executions by agents and tests appear here.")
        else:
            st.dataframe(
                [
                    {
                        "Time": e.at.strftime("%H:%M:%S"),
                        "Agent": e.agent,
                        "Tool": e.tool,
                        "Type": e.source,
                        "Status": e.status,
                        "ms": e.duration_ms,
                        "Detail": e.detail,
                    }
                    for e in c.tools.executions
                ],
                hide_index=True,
                width="stretch",
            )

    with tab_test:
        st.caption(
            "Run a built-in tool against the documents uploaded in this session."
        )
        name = st.selectbox("Tool", list(BUILTIN_BY_NAME), key="reg_test_tool")
        tool_obj = BUILTIN_BY_NAME[name].factory(c.document_context())
        schema = tool_obj.tool_spec["inputSchema"]["json"]
        st.json(schema, expanded=False)
        example = {
            k: "" if v.get("type") == "string" else 0
            for k, v in schema.get("properties", {}).items()
            if k in schema.get("required", [])
        }
        args_text = st.text_area(
            "Arguments (JSON)", value=json.dumps(example), key=f"reg_test_args_{name}"
        )
        if st.button("Run", key="reg_test_run"):
            try:
                args = json.loads(args_text or "{}")
                output = c.tools.execute_builtin(name, args, c.document_context())
                st.code(output[:20000], language="text")
            except Exception as exc:  # noqa: BLE001
                st.error(sanitize_error(exc))
