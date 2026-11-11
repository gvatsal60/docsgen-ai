"""MCP server configuration and lifecycle against the real demo MCP server (stdio and Streamable HTTP)."""

from __future__ import annotations

import json
import sys

import pytest

from models.mcp_models import AuthType, ConnectPolicy, MCPServerConfig, TransportType
from services.mcp_client_factory import build_headers


def test_config_validation_rules():
    with pytest.raises(ValueError):
        MCPServerConfig(id="Bad Id", name="x", command="python")
    with pytest.raises(ValueError):
        MCPServerConfig(
            id="web", name="Web", transport=TransportType.STREAMABLE_HTTP
        )  # URL missing
    with pytest.raises(ValueError):
        MCPServerConfig(
            id="sse",
            name="SSE",
            transport=TransportType.SSE,
            url="https://x/sse",
            auth_type=AuthType.OAUTH_CLIENT_CREDENTIALS,
            oauth_client_id="c",
        )
    with pytest.raises(ValueError):
        MCPServerConfig(
            id="hdr",
            name="H",
            transport=TransportType.SSE,
            url="https://x",
            headers={"Bad Header": "v"},
        )
    cfg = MCPServerConfig(id="files", name="Files", command="npx", args=["-y", "srv"])
    assert cfg.qualified_tool_name("read_file") == "files_read_file"


def test_headers_built_from_secrets(container):
    cfg = MCPServerConfig(
        id="api",
        name="API",
        transport=TransportType.STREAMABLE_HTTP,
        url="https://api.example.com/mcp",
        auth_type=AuthType.API_KEY_HEADER,
        api_key_header_name="X-Key",
        headers={"X-Plain": "1"},
        secret_header_names=["X-Secret"],
    )
    container.mcp_secrets.set("api", "api_key", "key-123456789", persist=False)
    container.mcp_secrets.set(
        "api",
        container.mcp_secrets.header_key("X-Secret"),
        "hidden-987654321",
        persist=True,
    )
    assert build_headers(cfg, container.mcp_secrets) == {
        "X-Plain": "1",
        "X-Secret": "hidden-987654321",
        "X-Key": "key-123456789",
    }
    bearer = cfg.model_copy(update={"auth_type": AuthType.BEARER})
    container.mcp_secrets.set("api", "bearer_token", "tok-abcdefgh", persist=False)
    assert (
        build_headers(bearer, container.mcp_secrets)["Authorization"]
        == "Bearer tok-abcdefgh"
    )


def test_stdio_connect_discover_disconnect_reconnect(container, demo_stdio_config):
    mcp = container.mcp
    mcp.save_server(demo_stdio_config, persist=True, is_new=True)
    with pytest.raises(ValueError):
        mcp.save_server(demo_stdio_config, persist=True, is_new=True)  # duplicate id
    test = mcp.test_server(demo_stdio_config)
    assert test.ok and test.tool_count == 4 and "lookup_company" in test.tool_names

    state = mcp.connect("demo")
    assert state.status == "connected" and state.tool_count == 4
    infos = {i.original_name: i for i in mcp.tool_infos("demo")}
    assert infos["lookup_company"].qualified_name == "demo_lookup_company"
    assert infos["lookup_company"].read_only is True and infos["lookup_company"].enabled
    assert (
        infos["delete_record"].requires_approval and not infos["delete_record"].enabled
    )  # destructive: off by default
    assert "name" in infos["lookup_company"].input_schema["properties"]

    mcp.disconnect("demo")
    assert (
        mcp.state("demo").status == "disconnected"
        and mcp.tool_infos() == []
        and mcp.available_tools() == {}
    )
    assert mcp.connect("demo").status == "connected"  # reconnect
    assert mcp.refresh_tools("demo").tool_count == 4


def test_tool_enable_disable_and_persistence(container, demo_stdio_config):
    mcp = container.mcp
    mcp.save_server(demo_stdio_config, persist=True, is_new=True)
    mcp.connect("demo")
    mcp.set_all_tools_enabled("demo", False)
    assert mcp.available_tools() == {}
    mcp.set_tools_enabled("demo", ["word_count", "current_date"], True)
    assert sorted(mcp.available_tools()) == ["demo_current_date", "demo_word_count"]
    saved = json.loads(container.settings.config_file.read_text())["mcp_servers"][0]
    assert (
        saved["tool_states"]["word_count"] is True
        and saved["tool_states"]["lookup_company"] is False
    )
    mcp.set_server_enabled("demo", False)
    assert mcp.state("demo").status == "disabled" and mcp.available_tools() == {}


def test_test_tool_requires_confirmation_for_destructive(container, demo_stdio_config):
    mcp = container.mcp
    mcp.save_server(demo_stdio_config, persist=False, is_new=True)
    mcp.connect("demo")
    ok = mcp.test_tool(
        "demo", "lookup_company", {"name": "Contoso Retail Group"}, confirmed=False
    )
    assert ok.ok and "Leeds" in ok.output
    blocked = mcp.test_tool(
        "demo", "delete_record", {"record_id": "R-1"}, confirmed=False
    )
    assert blocked.status == "blocked"
    done = mcp.test_tool("demo", "delete_record", {"record_id": "R-1"}, confirmed=True)
    assert done.ok and "deleted" in done.output


def test_connection_failures_are_reported(container):
    mcp = container.mcp
    bad_cmd = MCPServerConfig(
        id="badcmd",
        name="Bad",
        command=sys.executable,
        args=["/nonexistent/server.py"],
        startup_timeout=10,
    )
    mcp.save_server(bad_cmd, persist=False, is_new=True)
    state = mcp.connect("badcmd")
    assert state.status == "error" and state.last_error
    result = mcp.test_server(bad_cmd)
    assert not result.ok and result.diagnostics

    shell = MCPServerConfig(
        id="shell", name="Shell", command="bash", args=["-c", "echo hi"]
    )
    assert any("Shell" in p for p in mcp.validate(shell))

    http = MCPServerConfig(
        id="down",
        name="Down",
        transport=TransportType.STREAMABLE_HTTP,
        url="http://127.0.0.1:9/mcp",
        startup_timeout=5,
    )
    mcp.save_server(http, persist=False, is_new=True)
    assert mcp.connect("down").status == "error"

    remote_http = MCPServerConfig(
        id="plain",
        name="Plain",
        transport=TransportType.STREAMABLE_HTTP,
        url="http://mcp.example.com/mcp",
    )
    assert any("https" in p for p in mcp.validate(remote_http))


def test_streamable_http_with_bearer_auth(container, demo_http_server):
    mcp = container.mcp
    cfg = MCPServerConfig(
        id="web",
        name="Web demo",
        transport=TransportType.STREAMABLE_HTTP,
        url=demo_http_server["url"],
        auth_type=AuthType.BEARER,
        connect_policy=ConnectPolicy.MANUAL,
    )
    assert "A bearer token is required." in mcp.validate(cfg)
    mcp.secrets.set("web", "bearer_token", demo_http_server["token"], persist=True)
    mcp.save_server(cfg, persist=True, is_new=True)
    state = mcp.connect("web")
    assert state.status == "connected" and state.tool_count == 4
    assert mcp.test_tool("web", "current_date", {}, confirmed=False).ok
    mcp.secrets.set("web", "bearer_token", "wrong-token-000000", persist=False)
    mcp.connect("web")
    assert not mcp.test_tool(
        "web", "current_date", {}, confirmed=False
    ).ok  # server rejects the wrong token
    assert demo_http_server["token"] not in container.settings.config_file.read_text()


def test_startup_policy_and_restore(container, demo_stdio_config, data_dir):
    from services.container import ServiceContainer

    cfg = demo_stdio_config.model_copy(
        update={"connect_policy": ConnectPolicy.ON_STARTUP}
    )
    container.mcp.save_server(cfg, persist=True, is_new=True)
    container.mcp.set_tools_enabled("demo", ["word_count"], False)
    restored = ServiceContainer.create(data_dir=data_dir)
    try:
        states = restored.mcp.get_server("demo").tool_states
        assert (
            states["word_count"] is False
            and states["delete_record"] is False
            and states["lookup_company"] is True
        )
        assert (
            restored.mcp.state("demo").status == "connected"
        )  # connected by policy at startup
        assert "demo_word_count" not in restored.mcp.available_tools()
    finally:
        restored.shutdown()


def test_import_export(container):
    imported = container.mcp.import_config(
        json.dumps(
            {
                "mcpServers": {
                    "GitHub Tools": {
                        "command": "npx",
                        "args": ["-y", "server-github"],
                        "env": {"GITHUB_TOKEN": "ghp_supersecret12345", "LOG": "1"},
                    },
                    "Remote": {"url": "https://mcp.example.com/mcp"},
                }
            }
        ),
        persist=True,
    )
    assert sorted(imported) == ["github_tools", "remote"]
    gh = container.mcp.get_server("github_tools")
    assert gh.env == {"LOG": "1"} and gh.secret_env_keys == ["GITHUB_TOKEN"]
    assert (
        container.mcp.secrets.get("github_tools", "env:GITHUB_TOKEN")
        == "ghp_supersecret12345"
    )
    exported = container.mcp.export_config()
    assert (
        "ghp_supersecret12345" not in exported
        and "ghp_supersecret12345" not in container.settings.config_file.read_text()
    )
    container.mcp.delete_server("github_tools")
    assert (
        container.mcp.get_server("github_tools") is None
        and container.mcp.secrets.get("github_tools", "env:GITHUB_TOKEN") is None
    )
