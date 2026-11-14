"""MCP Tool Configuration page: add/edit/connect/test servers, discover and select tools, test tools."""

from __future__ import annotations

import json
import re

import streamlit as st

from models.mcp_models import (
    AUTH_LABELS,
    TRANSPORT_LABELS,
    AuthType,
    ConnectPolicy,
    MCPServerConfig,
    TransportType,
)
from services.container import ServiceContainer
from services.mcp_credential_manager import API_KEY, BEARER, OAUTH_SECRET
from services.security import sanitize_error
from ui.components import (
    badge,
    confirm_button,
    flash,
    format_kv_lines,
    page_header,
    parse_kv_lines,
    show_flashes,
)

FORM_KEY = "mcp_form_target"  # "new" | server id | None


def _status_badge(status: str) -> str:
    return badge(status.capitalize(), status)


def render(c: ServiceContainer) -> None:
    page_header(
        "MCP Server Configuration",
        "Connect external tools through the Model Context Protocol. Servers and tools"
        " are treated as untrusted until you enable and authorize them.",
        "🔌",
    )
    show_flashes()
    servers = c.mcp.list_servers()
    connected = [s for s in servers if c.mcp.state(s.id).status == "connected"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Configured servers", len(servers))
    m2.metric("Connected", len(connected))
    m3.metric("Discovered tools", len(c.mcp.tool_infos()))
    m4.metric("Enabled for agents", len(c.mcp.available_tools()))

    a1, a2, _ = st.columns([1, 1, 3])
    if a1.button("➕ Add MCP server", type="primary", width="stretch"):
        st.session_state[FORM_KEY] = "new"
    with a2.popover("Import / export", width="stretch"):
        _import_export(c)

    target = st.session_state.get(FORM_KEY)
    if target:
        _server_form(c, None if target == "new" else c.mcp.get_server(target))

    st.markdown("### Servers")
    if not servers:
        st.info(
            "No MCP servers configured yet. Add one, or try the bundled demo server:"
            " transport **stdio**, command **python**, argument"
            " **mcp_servers/demo_server.py**."
        )
    for cfg in servers:
        _server_card(c, cfg)

    st.markdown("### Discovered tools")
    _tool_selection(c)
    _tool_tester(c)


# ---------------------------------------------------------------------------------- cards
def _server_card(c: ServiceContainer, cfg: MCPServerConfig) -> None:
    state = c.mcp.state(cfg.id)
    with st.container(border=True):
        top = st.columns([3, 2, 2, 2])
        top[0].markdown(f"**{cfg.name}** · `{cfg.id}`")
        if cfg.description:
            top[0].caption(cfg.description)
        top[1].markdown(
            f"{_status_badge(state.status)}  \n{TRANSPORT_LABELS[cfg.transport]}"
        )
        enabled_tools = sum(1 for i in c.mcp.tool_infos(cfg.id) if i.enabled)
        top[2].markdown(
            f"**Tools:** {state.tool_count} discovered, {enabled_tools} enabled"
        )
        top[2].caption(
            "Saved permanently" if c.mcp.is_persisted(cfg.id) else "Session only"
        )
        top[3].markdown(
            f"**Server:** {'enabled' if cfg.enabled else 'disabled'}  \n**Connect:**"
            f" {cfg.connect_policy.value.replace('_', ' ')}"
        )
        if state.last_connected_at:
            top[3].caption(f"Last connected {state.last_connected_at:%H:%M:%S}")
        if state.last_error:
            st.error(f"Last error: {state.last_error}", icon="⚠️")

        b = st.columns(7)
        is_connected = state.status == "connected"
        if not is_connected:
            if b[0].button(
                "Connect",
                key=f"conn_{cfg.id}",
                disabled=not cfg.enabled,
                width="stretch",
                type="primary",
            ):
                with st.spinner(f"Connecting to {cfg.name}…"):
                    result = c.mcp.connect(cfg.id)
                flash(
                    (
                        f"{cfg.name}: connected with {result.tool_count} tool(s)."
                        if result.status == "connected"
                        else f"{cfg.name}: {result.last_error}"
                    ),
                    "success" if result.status == "connected" else "error",
                )
                st.rerun()
        elif b[0].button("Disconnect", key=f"disc_{cfg.id}", width="stretch"):
            c.mcp.disconnect(cfg.id)
            st.rerun()
        if b[1].button("Test", key=f"test_{cfg.id}", width="stretch"):
            with st.spinner("Testing connection…"):
                st.session_state[f"test_result_{cfg.id}"] = c.mcp.test_server(
                    cfg
                ).model_dump()
        if b[2].button(
            "Refresh tools",
            key=f"refresh_{cfg.id}",
            disabled=not is_connected,
            width="stretch",
        ):
            c.mcp.refresh_tools(cfg.id)
            st.rerun()
        if b[3].button("Edit", key=f"edit_{cfg.id}", width="stretch"):
            st.session_state[FORM_KEY] = cfg.id
            st.rerun()
        if b[4].button(
            "Disable" if cfg.enabled else "Enable",
            key=f"toggle_{cfg.id}",
            width="stretch",
        ):
            c.mcp.set_server_enabled(cfg.id, not cfg.enabled)
            st.rerun()
        with b[5]:
            if confirm_button(
                "Delete", key=f"del_{cfg.id}", confirm_label="Delete", width="stretch"
            ):
                c.mcp.delete_server(cfg.id)
                flash(f"Deleted server {cfg.name} and its stored credentials.", "info")
                st.rerun()
        if b[6].button(
            "Logs",
            key=f"logs_{cfg.id}",
            width="stretch",
            disabled=cfg.transport != TransportType.STDIO,
        ):
            st.session_state[f"show_logs_{cfg.id}"] = not st.session_state.get(
                f"show_logs_{cfg.id}", False
            )

        result = st.session_state.get(f"test_result_{cfg.id}")
        if result:
            (st.success if result["ok"] else st.error)(
                f"{result['message']} ({result['latency_ms']} ms)"
            )
            with st.expander("Diagnostics", expanded=not result["ok"]):
                st.code("\n".join(result["diagnostics"]) or "—", language="text")
                if result["tool_names"]:
                    st.caption("Tools: " + ", ".join(result["tool_names"]))
        if st.session_state.get(f"show_logs_{cfg.id}"):
            lines = c.mcp.connections.server_stderr_tail(cfg.id, 40)
            st.code("\n".join(lines) or "(no output)", language="text")


# ----------------------------------------------------------------------------------- form
def _secret_hint(c: ServiceContainer, server_id: str | None, name: str) -> str:
    if server_id and c.mcp_secrets.has(server_id, name):
        return f"Saved ({c.mcp_secrets.masked(server_id, name)}). Leave empty to keep."
    return ""


def _server_form(c: ServiceContainer, existing: MCPServerConfig | None) -> None:
    is_new = existing is None
    fk = "mcpf_" + (existing.id if existing is not None else "new")
    e = existing
    with st.container(border=True):
        st.markdown("#### " + (f"Edit {e.name}" if e is not None else "Add MCP server"))
        st.caption("Basic configuration")
        r = st.columns([2, 1, 1])
        name = r[0].text_input(
            "Server name *", value=e.name if e else "", key=f"{fk}_name"
        )
        suggested = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")[:32] or ""
        server_id = r[1].text_input(
            "Server identifier *",
            value=e.id if e else suggested,
            key=f"{fk}_id",
            disabled=not is_new,
            help="Lowercase letters, digits, underscores. Used to prefix tool names.",
        )
        enabled = r[2].toggle(
            "Enabled", value=e.enabled if e else True, key=f"{fk}_enabled"
        )
        description = st.text_input(
            "Description", value=e.description if e else "", key=f"{fk}_desc"
        )

        st.caption("Transport")
        transports = list(TransportType)
        transport = st.selectbox(
            "Transport type",
            transports,
            index=transports.index(e.transport) if e else 0,
            format_func=lambda t: TRANSPORT_LABELS[t],
            key=f"{fk}_transport",
        )

        secrets_to_store: dict[str, str] = {}
        values: dict = {}
        if transport == TransportType.STDIO:
            r = st.columns([1, 2])
            values["command"] = r[0].text_input(
                "Command *",
                value=(e.command or "") if e else "",
                key=f"{fk}_cmd",
                placeholder="python, uvx, npx, docker…",
                help=(
                    "Executed directly (never through a shell). Must be on the allow"
                    " list in Application Settings."
                ),
            )
            args_text = r[1].text_area(
                "Arguments (one per line)",
                value="\n".join(e.args) if e else "",
                key=f"{fk}_args",
                height=90,
            )
            values["args"] = [
                a for a in (line.strip() for line in args_text.splitlines()) if a
            ]
            values["cwd"] = (
                st.text_input(
                    "Working directory (optional)",
                    value=(e.cwd or "") if e else "",
                    key=f"{fk}_cwd",
                )
                or None
            )
            env_text = st.text_area(
                "Environment variables (NAME=value, one per line, not secret)",
                value=format_kv_lines(e.env) if e else "",
                key=f"{fk}_env",
                height=80,
            )
            st.markdown(
                "**Secret environment variables** — stored encrypted or for this"
                " session only, never shown again"
            )
            secret_names = list(e.secret_env_keys) if e else []
            keep = []
            for env_name in secret_names:
                cols = st.columns([2, 3, 1])
                cols[0].text_input(
                    "Name",
                    value=env_name,
                    disabled=True,
                    key=f"{fk}_senv_name_{env_name}",
                    label_visibility="collapsed",
                )
                hint = _secret_hint(
                    c, e.id if e else None, c.mcp_secrets.env_key(env_name)
                )
                new_val = cols[1].text_input(
                    "Value",
                    type="password",
                    key=f"{fk}_senv_val_{env_name}",
                    placeholder=hint or "value",
                    label_visibility="collapsed",
                )
                remove = cols[2].checkbox("Remove", key=f"{fk}_senv_rm_{env_name}")
                if not remove:
                    keep.append(env_name)
                    if new_val:
                        secrets_to_store[c.mcp_secrets.env_key(env_name)] = new_val
            cols = st.columns([2, 3, 1])
            add_name = cols[0].text_input(
                "New secret variable name",
                key=f"{fk}_senv_new_name",
                placeholder="e.g. GITHUB_TOKEN",
            )
            add_val = cols[1].text_input(
                "New secret value", type="password", key=f"{fk}_senv_new_val"
            )
            if add_name and add_val:
                keep.append(add_name.strip())
                secrets_to_store[c.mcp_secrets.env_key(add_name.strip())] = add_val
            values["secret_env_keys"] = keep
        else:
            values["url"] = st.text_input(
                "Server URL *",
                value=(e.url or "") if e else "",
                key=f"{fk}_url",
                placeholder=(
                    "https://example.com/mcp"
                    if transport == TransportType.STREAMABLE_HTTP
                    else "https://example.com/sse"
                ),
            )
            auth_options = [
                a
                for a in AuthType
                if transport == TransportType.STREAMABLE_HTTP
                or a != AuthType.OAUTH_CLIENT_CREDENTIALS
            ]
            current_auth = (
                e.auth_type if e and e.auth_type in auth_options else AuthType.NONE
            )
            auth = st.selectbox(
                "Authentication method",
                auth_options,
                index=auth_options.index(current_auth),
                format_func=lambda a: AUTH_LABELS[a],
                key=f"{fk}_auth",
            )
            values["auth_type"] = auth
            sid = e.id if e else None
            if auth == AuthType.BEARER:
                v = st.text_input(
                    "Bearer token",
                    type="password",
                    key=f"{fk}_bearer",
                    placeholder=_secret_hint(c, sid, BEARER) or "token",
                )
                st.caption(_secret_hint(c, sid, BEARER))
                if v:
                    secrets_to_store[BEARER] = v
            elif auth == AuthType.API_KEY_HEADER:
                cols = st.columns(2)
                values["api_key_header_name"] = cols[0].text_input(
                    "Header name",
                    value=e.api_key_header_name if e else "X-API-Key",
                    key=f"{fk}_apihdr",
                )
                v = cols[1].text_input(
                    "API key",
                    type="password",
                    key=f"{fk}_apikey",
                    placeholder=_secret_hint(c, sid, API_KEY) or "key",
                )
                if v:
                    secrets_to_store[API_KEY] = v
            elif auth == AuthType.OAUTH_CLIENT_CREDENTIALS:
                cols = st.columns(3)
                values["oauth_client_id"] = cols[0].text_input(
                    "Client ID",
                    value=(e.oauth_client_id or "") if e else "",
                    key=f"{fk}_oid",
                )
                v = cols[1].text_input(
                    "Client secret",
                    type="password",
                    key=f"{fk}_osecret",
                    placeholder=_secret_hint(c, sid, OAUTH_SECRET) or "secret",
                )
                if v:
                    secrets_to_store[OAUTH_SECRET] = v
                scopes = cols[2].text_input(
                    "Scopes (space separated)",
                    value=" ".join(e.oauth_scopes) if e else "",
                    key=f"{fk}_oscopes",
                )
                values["oauth_scopes"] = scopes.split()
            hdr_text = st.text_area(
                "Additional HTTP headers (Name: value, one per line, not secret)",
                value=format_kv_lines(e.headers, ": ") if e else "",
                key=f"{fk}_headers",
                height=70,
            )
            st.markdown("**Secret headers** (e.g. a custom API token header)")
            keep = []
            for hname in list(e.secret_header_names) if e else []:
                cols = st.columns([2, 3, 1])
                cols[0].text_input(
                    "Header",
                    value=hname,
                    disabled=True,
                    key=f"{fk}_shdr_name_{hname}",
                    label_visibility="collapsed",
                )
                hint = _secret_hint(
                    c, e.id if e else None, c.mcp_secrets.header_key(hname)
                )
                new_val = cols[1].text_input(
                    "Value",
                    type="password",
                    key=f"{fk}_shdr_val_{hname}",
                    placeholder=hint or "value",
                    label_visibility="collapsed",
                )
                if not cols[2].checkbox("Remove", key=f"{fk}_shdr_rm_{hname}"):
                    keep.append(hname)
                    if new_val:
                        secrets_to_store[c.mcp_secrets.header_key(hname)] = new_val
            cols = st.columns([2, 3, 1])
            add_h = cols[0].text_input(
                "New secret header name",
                key=f"{fk}_shdr_new_name",
                placeholder="e.g. X-Auth-Token",
            )
            add_hv = cols[1].text_input(
                "New secret header value", type="password", key=f"{fk}_shdr_new_val"
            )
            if add_h and add_hv:
                keep.append(add_h.strip())
                secrets_to_store[c.mcp_secrets.header_key(add_h.strip())] = add_hv
            values["secret_header_names"] = keep

        st.caption("Execution")
        r = st.columns(4)
        startup_timeout = r[0].number_input(
            "Connection timeout (s)",
            5,
            300,
            value=e.startup_timeout if e else c.settings.mcp_startup_timeout,
            key=f"{fk}_stimeout",
        )
        tool_timeout = r[1].number_input(
            "Tool timeout (s)",
            5,
            900,
            value=e.tool_timeout if e else c.settings.mcp_tool_timeout,
            key=f"{fk}_ttimeout",
        )
        policies = list(ConnectPolicy)
        policy = r[2].selectbox(
            "Connection policy",
            policies,
            index=policies.index(e.connect_policy) if e else 0,
            format_func=lambda p: (
                "Connect manually"
                if p == ConnectPolicy.MANUAL
                else "Connect on startup"
            ),
            key=f"{fk}_policy",
        )
        prefix = r[3].text_input(
            "Tool name prefix",
            value=(e.tool_prefix or "") if e else "",
            key=f"{fk}_prefix",
            placeholder=server_id or "server id",
        )

        persist = (
            st.radio(
                "Save configuration",
                ["Persist (secrets encrypted)", "Session only"],
                index=0 if (e is None or c.mcp.is_persisted(e.id)) else 1,
                horizontal=True,
                key=f"{fk}_persist",
            )
            == "Persist (secrets encrypted)"
        )

        b = st.columns([1, 1, 1, 3])
        save = b[0].button("Save", type="primary", key=f"{fk}_save", width="stretch")
        save_connect = b[1].button(
            "Save & connect", key=f"{fk}_save_connect", width="stretch"
        )
        if b[2].button("Cancel", key=f"{fk}_cancel", width="stretch"):
            st.session_state.pop(FORM_KEY, None)
            st.rerun()

        if save or save_connect:
            try:
                if transport == TransportType.STDIO:
                    values["env"] = parse_kv_lines(env_text)
                else:
                    values["headers"] = parse_kv_lines(hdr_text, ":")
                cfg = MCPServerConfig(
                    id=server_id,
                    name=name,
                    description=description,
                    enabled=enabled,
                    transport=transport,
                    startup_timeout=int(startup_timeout),
                    tool_timeout=int(tool_timeout),
                    connect_policy=policy,
                    tool_prefix=prefix or None,
                    tool_states=e.tool_states if e else {},
                    **values,
                )
            except ValueError as exc:
                st.error(sanitize_error(exc))
                return
            for secret_name, secret_value in secrets_to_store.items():
                c.mcp_secrets.set(cfg.id, secret_name, secret_value, persist=persist)
            problems = c.mcp.validate(cfg)
            if problems:
                for p in problems:
                    st.error(p)
                return
            try:
                c.mcp.save_server(cfg, persist=persist, is_new=is_new)
            except ValueError as exc:
                st.error(str(exc))
                return
            st.session_state.pop(FORM_KEY, None)
            for key in [k for k in st.session_state if str(k).startswith(fk)]:
                st.session_state.pop(key, None)
            if save_connect and cfg.enabled:
                with st.spinner("Connecting…"):
                    state = c.mcp.connect(cfg.id)
                flash(
                    (
                        f"Saved and connected: {state.tool_count} tool(s) discovered."
                        if state.status == "connected"
                        else f"Saved, but the connection failed: {state.last_error}"
                    ),
                    "success" if state.status == "connected" else "error",
                )
            else:
                flash(
                    f"Server '{cfg.name}'"
                    f" saved{' permanently' if persist else ' for this session'}."
                )
            st.rerun()


# ------------------------------------------------------------------------------ tools
def _tool_selection(c: ServiceContainer) -> None:
    infos = c.mcp.tool_infos()
    if not infos:
        st.caption("Connect a server to discover its tools.")
        return
    f1, f2, f3 = st.columns([2, 2, 1])
    query = f1.text_input(
        "Search tools", key="mcp_tool_search", placeholder="name or description"
    )
    server_ids = sorted({i.server_id for i in infos})
    server_filter = f2.selectbox(
        "Filter by server", ["All servers"] + server_ids, key="mcp_tool_server"
    )
    only_enabled = f3.toggle("Enabled only", key="mcp_tool_only_enabled")
    q = query.lower().strip()
    filtered = [
        i
        for i in infos
        if (server_filter == "All servers" or i.server_id == server_filter)
        and (not q or q in i.qualified_name.lower() or q in i.description.lower())
        and (not only_enabled or i.enabled)
    ]
    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("Enable all (filtered)", width="stretch"):
        for sid in {i.server_id for i in filtered}:
            c.mcp.set_tools_enabled(
                sid, [i.original_name for i in filtered if i.server_id == sid], True
            )
        st.rerun()
    if b2.button("Disable all (filtered)", width="stretch"):
        for sid in {i.server_id for i in filtered}:
            c.mcp.set_tools_enabled(
                sid, [i.original_name for i in filtered if i.server_id == sid], False
            )
        st.rerun()

    rows = [
        {
            "Enabled": i.enabled,
            "Tool": i.qualified_name,
            "MCP server": i.server_name,
            "Risk": (
                "⚠️ needs approval"
                if i.requires_approval
                else ("read-only" if i.read_only else "—")
            ),
            "Description": i.description[:200],
        }
        for i in filtered
    ]
    edited = st.data_editor(
        rows,
        key="mcp_tools_editor",
        hide_index=True,
        width="stretch",
        disabled=["Tool", "MCP server", "Risk", "Description"],
        column_config={"Enabled": st.column_config.CheckboxColumn(width="small")},
    )
    changes = [
        (i, row["Enabled"])
        for i, row in zip(filtered, edited)
        if bool(row["Enabled"]) != i.enabled
    ]
    if changes:
        for info, enabled in changes:
            c.mcp.set_tools_enabled(info.server_id, [info.original_name], bool(enabled))
        st.session_state.pop("mcp_tools_editor", None)
        st.rerun()
    st.caption(
        "Only enabled tools on connected servers can be offered to agents, and only to"
        " agents you authorize on the **Tool Registry** page. Tools marked “needs"
        " approval” are additionally blocked during a workflow run unless you approve"
        " them for that run."
    )
    with st.expander("Input schemas"):
        for i in filtered:
            st.markdown(f"**{i.qualified_name}** — {i.approval_reason or ''}")
            st.json(i.input_schema, expanded=False)


def _tool_tester(c: ServiceContainer) -> None:
    infos = c.mcp.tool_infos()
    if not infos:
        return
    with st.expander("🧪 Test a tool"):
        st.warning(
            "Testing a tool sends a real request to the external MCP server and may"
            " perform an external operation."
        )
        names = [i.qualified_name for i in infos]
        chosen = st.selectbox("Tool", names, key="mcp_test_tool")
        info = next(i for i in infos if i.qualified_name == chosen)
        st.caption(info.description)
        props = (info.input_schema or {}).get("properties", {})
        example = {k: "" if v.get("type") == "string" else 0 for k, v in props.items()}
        args_text = st.text_area(
            "Arguments (JSON)",
            value=json.dumps(example, indent=1),
            key=f"mcp_test_args_{chosen}",
            height=120,
        )
        confirmed = True
        if info.requires_approval:
            st.error(
                "This tool may change data or perform an external action:"
                f" {info.approval_reason}"
            )
            confirmed = st.checkbox(
                "I understand and want to run this tool",
                key=f"mcp_test_confirm_{chosen}",
            )
        if st.button("Run tool", key="mcp_test_run", disabled=not confirmed):
            try:
                args = json.loads(args_text or "{}")
            except ValueError as exc:
                st.error(f"Invalid JSON arguments: {exc}")
                return
            if not isinstance(args, dict):
                st.error("Invalid JSON arguments: Arguments must be a JSON object.")
                return
            with st.spinner("Calling tool…"):
                result = c.mcp.test_tool(
                    info.server_id, info.original_name, args, confirmed=confirmed
                )
            (st.success if result.ok else st.error)(
                f"Status: {result.status} ({result.latency_ms or 0} ms)"
            )
            st.code(
                result.output or "(empty)",
                language=(
                    "json" if result.output.strip().startswith(("{", "[")) else "text"
                ),
            )


def _import_export(c: ServiceContainer) -> None:
    st.markdown("**Export** (secrets are never exported)")
    st.download_button(
        "Download configuration JSON",
        c.mcp.export_config(),
        file_name="docsgen_mcp_servers.json",
        mime="application/json",
    )
    st.markdown("**Import** DocsGen JSON or a standard `mcpServers` file")
    uploaded = st.file_uploader(
        "Configuration file", type=["json"], key="mcp_import_file"
    )
    persist = st.checkbox(
        "Persist imported servers", value=True, key="mcp_import_persist"
    )
    if uploaded is not None and st.button("Import", key="mcp_import_btn"):
        try:
            data = uploaded.getvalue()
            if len(data) > 512 * 1024:
                raise ValueError("The file is too large.")
            ids = c.mcp.import_config(data.decode("utf-8"), persist=persist)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Import failed: {sanitize_error(exc)}")
        else:
            flash(
                f"Imported {len(ids)} server(s): {', '.join(ids)}. Review each one"
                " before connecting."
            )
            st.rerun()
