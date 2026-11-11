"""Registry of discovered MCP tools and their enabled/disabled state."""

from __future__ import annotations

from models.mcp_models import MCPServerConfig, MCPToolInfo
from services.mcp_connection_manager import MCPConnectionManager
from services.mcp_tool_discovery import SafeMCPAgentTool


class MCPToolRegistry:
    """Combines live tool objects with the persisted per-tool enable flags."""

    def __init__(self, connections: MCPConnectionManager, config_lookup) -> None:
        self._connections = connections
        self._config_lookup = (
            config_lookup  # callable(server_id) -> MCPServerConfig | None
        )

    def tool_infos(self, server_id: str | None = None) -> list[MCPToolInfo]:
        infos: list[MCPToolInfo] = []
        ids = [server_id] if server_id else self._connections.connected_ids()
        for sid in ids:
            cfg = self._config_lookup(sid)
            if cfg is None:
                continue
            for _, info in self._connections.tools(sid):
                state = cfg.tool_states.get(info.original_name)
                if state is not None:
                    info.enabled = state
                infos.append(info)
        return infos

    def available_tools(self) -> dict[str, tuple[SafeMCPAgentTool, MCPToolInfo]]:
        """Tools from connected, enabled servers whose tool flag is enabled, keyed by qualified name."""
        result: dict[str, tuple[SafeMCPAgentTool, MCPToolInfo]] = {}
        for sid in self._connections.connected_ids():
            cfg = self._config_lookup(sid)
            if cfg is None or not cfg.enabled:
                continue
            for tool, info in self._connections.tools(sid):
                enabled = cfg.tool_states.get(info.original_name, info.enabled)
                if enabled:
                    info.enabled = True
                    result[info.qualified_name] = (tool, info)
        return result

    @staticmethod
    def set_enabled(
        cfg: MCPServerConfig, original_names: list[str], enabled: bool
    ) -> MCPServerConfig:
        states = dict(cfg.tool_states)
        for name in original_names:
            states[name] = enabled
        return cfg.model_copy(update={"tool_states": states})
