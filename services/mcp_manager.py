"""MCP Manager: server configuration CRUD, persistence, lifecycle and tool selection (facade)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from models.mcp_models import (
    ConnectPolicy,
    MCPServerConfig,
    MCPServerState,
    MCPTestResult,
    MCPToolInfo,
    ToolTestResult,
)
from services.mcp_client_factory import ClientPolicy, validate_server_config
from services.mcp_connection_manager import MCPConnectionManager
from services.mcp_credential_manager import MCPCredentialManager
from services.mcp_tool_discovery import SafeMCPAgentTool
from services.mcp_tool_registry import MCPToolRegistry
from services.security import sanitize_error
from storage.configuration_store import ConfigurationStore

logger = logging.getLogger(__name__)


class MCPManager:
    def __init__(
        self,
        config_store: ConfigurationStore,
        secrets: MCPCredentialManager,
        policy_provider: Callable[[], ClientPolicy],
        connections: MCPConnectionManager,
        default_enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self._store = config_store
        self.secrets = secrets
        self._policy_provider = policy_provider
        self.connections = connections
        self._default_enabled = default_enabled
        self._session_servers: dict[
            str, MCPServerConfig
        ] = {}  # session-only configurations
        self.tools = MCPToolRegistry(connections, self.get_server)

    # ------------------------------------------------------------------ configuration
    def _persisted(self) -> dict[str, MCPServerConfig]:
        result: dict[str, MCPServerConfig] = {}
        for raw in self._store.get("mcp_servers", []) or []:
            try:
                cfg = MCPServerConfig(**raw)
                result[cfg.id] = cfg
            except ValueError as exc:
                logger.warning(
                    "Ignoring invalid saved MCP server: %s", sanitize_error(exc)
                )
        return result

    def list_servers(self) -> list[MCPServerConfig]:
        servers = self._persisted()
        servers.update(self._session_servers)
        return sorted(servers.values(), key=lambda c: c.name.lower())

    def get_server(self, server_id: str) -> MCPServerConfig | None:
        if server_id in self._session_servers:
            return self._session_servers[server_id]
        return self._persisted().get(server_id)

    def is_persisted(self, server_id: str) -> bool:
        return server_id in self._persisted()

    def save_server(self, cfg: MCPServerConfig, *, persist: bool, is_new: bool) -> None:
        if is_new and self.get_server(cfg.id) is not None:
            raise ValueError(f"A server with identifier '{cfg.id}' already exists.")
        if persist:
            servers = self._persisted()
            servers[cfg.id] = cfg
            self._session_servers.pop(cfg.id, None)
            self._write(servers)
        else:
            self._session_servers[cfg.id] = cfg
            if self.is_persisted(cfg.id):
                servers = self._persisted()
                servers.pop(cfg.id)
                self._write(servers)

    def _write(self, servers: dict[str, MCPServerConfig]) -> None:
        self._store.update(
            "mcp_servers", [s.model_dump(mode="json") for s in servers.values()]
        )

    def _update(self, cfg: MCPServerConfig) -> None:
        self.save_server(
            cfg,
            persist=self.is_persisted(cfg.id) or cfg.id not in self._session_servers,
            is_new=False,
        )

    def delete_server(self, server_id: str) -> None:
        self.connections.forget(server_id)
        self._session_servers.pop(server_id, None)
        servers = self._persisted()
        if server_id in servers:
            servers.pop(server_id)
            self._write(servers)
        self.secrets.clear(server_id)

    def set_server_enabled(self, server_id: str, enabled: bool) -> None:
        cfg = self._require(server_id)
        self._update(cfg.model_copy(update={"enabled": enabled}))
        if not enabled:
            self.connections.disconnect(server_id)
            self.connections.state(server_id).status = "disabled"
        elif self.connections.state(server_id).status == "disabled":
            self.connections.state(server_id).status = "disconnected"

    def validate(self, cfg: MCPServerConfig) -> list[str]:
        return validate_server_config(cfg, self._policy_provider(), self.secrets)

    def _require(self, server_id: str) -> MCPServerConfig:
        cfg = self.get_server(server_id)
        if cfg is None:
            raise KeyError(f"Unknown MCP server: {server_id}")
        return cfg

    # ------------------------------------------------------------------ lifecycle
    def connect(self, server_id: str) -> MCPServerState:
        cfg = self._require(server_id)
        if not cfg.enabled:
            st = self.connections.state(server_id)
            st.status, st.last_error = (
                "disabled",
                "Enable the server before connecting.",
            )
            return st
        state = self.connections.connect(cfg, default_enabled=self._default_enabled())
        if state.status == "connected":
            self._record_tool_defaults(cfg)
        return state

    def _record_tool_defaults(self, cfg: MCPServerConfig) -> None:
        """Persist the default enabled flag of newly discovered tools so the choice is stable."""
        states = dict(cfg.tool_states)
        changed = False
        for _, info in self.connections.tools(cfg.id):
            if info.original_name not in states:
                states[info.original_name] = info.enabled
                changed = True
        if changed:
            self._update(cfg.model_copy(update={"tool_states": states}))

    def disconnect(self, server_id: str) -> None:
        self.connections.disconnect(server_id)

    def refresh_tools(self, server_id: str) -> MCPServerState:
        cfg = self._require(server_id)
        state = self.connections.refresh_tools(
            cfg, default_enabled=self._default_enabled()
        )
        self._record_tool_defaults(cfg)
        return state

    def test_server(self, cfg: MCPServerConfig) -> MCPTestResult:
        return self.connections.test(cfg)

    def connect_startup_servers(self) -> dict[str, MCPServerState]:
        results = {}
        for cfg in self.list_servers():
            if cfg.enabled and cfg.connect_policy == ConnectPolicy.ON_STARTUP:
                results[cfg.id] = self.connect(cfg.id)
        return results

    def state(self, server_id: str) -> MCPServerState:
        cfg = self.get_server(server_id)
        st = self.connections.state(server_id)
        if cfg is not None and not cfg.enabled:
            st.status = "disabled"
        # refresh broken-connection detection
        self.connections.is_connected(server_id)
        return st

    # ------------------------------------------------------------------ tools
    def tool_infos(self, server_id: str | None = None) -> list[MCPToolInfo]:
        return self.tools.tool_infos(server_id)

    def set_tools_enabled(
        self, server_id: str, original_names: list[str], enabled: bool
    ) -> None:
        cfg = self._require(server_id)
        self._update(MCPToolRegistry.set_enabled(cfg, original_names, enabled))

    def set_all_tools_enabled(self, server_id: str, enabled: bool) -> None:
        names = [i.original_name for i in self.tool_infos(server_id)]
        self.set_tools_enabled(server_id, names, enabled)

    def available_tools(self) -> dict[str, tuple[SafeMCPAgentTool, MCPToolInfo]]:
        return self.tools.available_tools()

    def test_tool(
        self,
        server_id: str,
        original_name: str,
        arguments: dict[str, Any],
        *,
        confirmed: bool,
    ) -> ToolTestResult:
        """Run a tool once from the UI. Consequential tools require explicit confirmation."""
        info = next(
            (i for i in self.tool_infos(server_id) if i.original_name == original_name),
            None,
        )
        if info is None:
            return ToolTestResult(
                ok=False, status="error", output="Tool not found on a connected server."
            )
        if info.requires_approval and not confirmed:
            return ToolTestResult(
                ok=False,
                status="blocked",
                output=(
                    f"Blocked: {info.approval_reason} Confirm that you want to run it."
                ),
            )
        cfg = self._require(server_id)
        started = time.monotonic()
        try:
            status, text = self.connections.call_tool(
                server_id, original_name, arguments, cfg.tool_timeout
            )
        except Exception as exc:  # noqa: BLE001
            return ToolTestResult(
                ok=False,
                status="error",
                output=sanitize_error(exc),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        return ToolTestResult(
            ok=status == "success",
            status="success" if status == "success" else "error",
            output=text[:20_000],
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    # ------------------------------------------------------------------ import / export
    def export_config(self) -> str:
        """Export non-secret server configuration as JSON (secrets are never exported)."""
        return json.dumps(
            {"mcpServers": [s.model_dump(mode="json") for s in self.list_servers()]},
            indent=2,
        )

    def import_config(self, text: str, *, persist: bool) -> list[str]:
        """Import servers from DocsGen JSON or the common ``{"mcpServers": {name: {...}}}`` format."""
        data = json.loads(text)
        servers = data.get("mcpServers", data)
        imported: list[str] = []
        items: list[tuple[str | None, dict]] = (
            [(None, s) for s in servers]
            if isinstance(servers, list)
            else list(servers.items())
        )
        for key, raw in items:
            raw = dict(raw)
            if "id" not in raw:
                # Claude Desktop / Strands load_servers style entry
                raw = _convert_external_entry(key or "server", raw)
            # Never keep secret-looking environment values in the plain configuration file.
            env = dict(raw.get("env") or {})
            secret_keys = set(raw.get("secret_env_keys") or [])
            for env_name in list(env):
                if any(
                    h in env_name.lower()
                    for h in ("key", "token", "secret", "password", "credential")
                ):
                    value = str(env.pop(env_name))
                    if value and not value.startswith("${"):
                        self.secrets.set(
                            raw["id"],
                            MCPCredentialManager.env_key(env_name),
                            value,
                            persist=persist,
                        )
                    secret_keys.add(env_name)
            raw["env"], raw["secret_env_keys"] = env, sorted(secret_keys)
            cfg = MCPServerConfig(**raw)
            self.save_server(
                cfg, persist=persist, is_new=self.get_server(cfg.id) is None
            )
            imported.append(cfg.id)
        return imported


def _convert_external_entry(name: str, raw: dict) -> dict:
    import re

    sid = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "server"
    if not sid[0].isalpha():
        sid = "s_" + sid
    entry: dict[str, Any] = {"id": sid[:32], "name": name}
    if "command" in raw:
        entry.update(
            transport="stdio",
            command=raw["command"],
            args=raw.get("args", []),
            cwd=raw.get("cwd"),
        )
        env = raw.get("env") or {}
        # Values that reference environment placeholders are kept as plain values (no secrets imported).
        entry["env"] = {k: str(v) for k, v in env.items()}
    else:
        transport = raw.get("transport") or raw.get("type") or "streamable_http"
        entry.update(
            transport="sse" if "sse" in str(transport) else "streamable_http",
            url=raw.get("url"),
        )
    return entry
