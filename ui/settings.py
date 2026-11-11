"""Application Settings page."""

from __future__ import annotations

import streamlit as st

from services.container import ServiceContainer
from services.security import sanitize_error
from ui.components import confirm_button, flash, page_header, show_flashes


def render(c: ServiceContainer) -> None:
    page_header(
        "Application Settings",
        "Limits, timeouts and security policies. Saved settings apply to all sessions"
        " of this installation.",
        "⚙️",
    )
    show_flashes()
    s = c.settings
    with st.form("settings_form"):
        st.markdown("#### Uploads")
        r = st.columns(3)
        max_upload_mb = r[0].number_input(
            "Maximum file size (MB)", 1, 500, s.max_upload_mb
        )
        max_source_files = r[1].number_input(
            "Maximum source files", 1, 200, s.max_source_files
        )
        retention = r[2].number_input(
            "Delete uploads older than (hours)", 1, 720, s.upload_retention_hours
        )
        st.markdown("#### Agents")
        r = st.columns(3)
        agent_timeout = r[0].number_input(
            "Agent timeout (s)", 10, 3600, s.agent_timeout_seconds
        )
        context_chars = r[1].number_input(
            "Source characters sent per agent",
            5_000,
            1_000_000,
            s.agent_context_chars,
            step=5_000,
        )
        tool_chars = r[2].number_input(
            "Maximum tool result size (chars)",
            1_000,
            200_000,
            s.tool_result_max_chars,
            step=1_000,
        )
        st.markdown("#### MCP security")
        r = st.columns(3)
        mcp_startup = r[0].number_input(
            "Default connection timeout (s)", 5, 300, s.mcp_startup_timeout
        )
        mcp_tool = r[1].number_input(
            "Default tool timeout (s)", 5, 900, s.mcp_tool_timeout
        )
        new_tools_enabled = r[2].toggle(
            "Enable newly discovered read-only tools",
            s.new_tools_enabled_by_default,
            help="Consequential tools are always disabled when first discovered.",
        )
        allowlist = st.text_input(
            "Allowed stdio commands (comma separated)",
            ", ".join(s.stdio_command_allowlist),
            help=(
                "MCP stdio servers may only start these executables. Shell interpreters"
                " are always refused."
            ),
        )
        r = st.columns(2)
        http_local = r[0].toggle(
            "Allow plain HTTP to localhost", s.allow_insecure_http_localhost
        )
        http_remote = r[1].toggle(
            "Allow plain HTTP to remote hosts (not recommended)",
            s.allow_insecure_http_remote,
        )
        st.markdown("#### Credentials and logging")
        r = st.columns(2)
        persist_default = r[0].toggle(
            "Default to encrypted persistent credentials", s.default_persist_credentials
        )
        log_level = r[1].selectbox(
            "Log level",
            ["DEBUG", "INFO", "WARNING", "ERROR"],
            index=["DEBUG", "INFO", "WARNING", "ERROR"].index(s.log_level),
        )
        if st.form_submit_button("Save settings", type="primary"):
            try:
                c.update_settings(
                    {
                        "max_upload_mb": int(max_upload_mb),
                        "max_source_files": int(max_source_files),
                        "upload_retention_hours": int(retention),
                        "agent_timeout_seconds": int(agent_timeout),
                        "agent_context_chars": int(context_chars),
                        "tool_result_max_chars": int(tool_chars),
                        "mcp_startup_timeout": int(mcp_startup),
                        "mcp_tool_timeout": int(mcp_tool),
                        "new_tools_enabled_by_default": new_tools_enabled,
                        "stdio_command_allowlist": [
                            x.strip() for x in allowlist.split(",") if x.strip()
                        ],
                        "allow_insecure_http_localhost": http_local,
                        "allow_insecure_http_remote": http_remote,
                        "default_persist_credentials": persist_default,
                        "log_level": log_level,
                    }
                )
                c.files.max_bytes = c.settings.max_upload_mb * 1024 * 1024
            except Exception as exc:  # noqa: BLE001
                st.error(sanitize_error(exc))
            else:
                flash("Settings saved.")
                st.rerun()

    st.markdown("#### Stored credentials")
    keys = c.credential_store.keys()
    st.caption(
        f"{len(keys)} credential(s) available to this session (names only, values are"
        " never shown): " + (", ".join(keys) if keys else "none")
    )
    if confirm_button(
        "Delete all saved credentials",
        key="wipe_creds",
        confirm_label="Delete all credentials",
    ):
        removed = c.credential_store.delete_prefix("")
        flash(f"Deleted {removed} credential(s).", "info")
        st.rerun()

    st.markdown("#### Storage")
    st.code(
        f"Data directory: {s.data_dir}\nConfiguration: "
        f" {s.config_file.name}\nCredentials:    {s.credentials_file.name}"
        f" (Fernet-encrypted)\nGenerated docs: {s.generated_dir}",
        language="text",
    )
    if confirm_button(
        "Reset this session (uploads, workflow, history, MCP connections)",
        key="reset_session",
        confirm_label="Reset session",
    ):
        from storage.session_store import reset_container

        reset_container()
        st.rerun()
