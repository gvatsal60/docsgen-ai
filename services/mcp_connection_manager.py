"""Lifecycle management for live MCP connections (connect, disconnect, reconnect, health)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from strands.tools.mcp import MCPClient

from models.mcp_models import (
    MCPServerConfig,
    MCPServerState,
    MCPTestResult,
    MCPToolInfo,
)
from services.mcp_client_factory import ClientPolicy, build_client
from services.mcp_credential_manager import MCPCredentialManager
from services.mcp_tool_discovery import SafeMCPAgentTool, discover_tools
from services.security import Redactor, sanitize_error

logger = logging.getLogger(__name__)


class MCPConnectionManager:
    """Owns the ``MCPClient`` objects for one application session."""

    def __init__(
        self, secrets: MCPCredentialManager, policy_provider, log_dir: Path
    ) -> None:
        self._secrets = secrets
        self._policy_provider = policy_provider  # callable returning ClientPolicy
        self._log_dir = log_dir
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, list[tuple[SafeMCPAgentTool, MCPToolInfo]]] = {}
        self.states: dict[str, MCPServerState] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ helpers
    def _policy(self) -> ClientPolicy:
        return self._policy_provider()

    def _stderr_path(self, server_id: str) -> Path:
        return self._log_dir / f"mcp_{server_id}.stderr.log"

    def server_stderr_tail(self, server_id: str, lines: int = 20) -> list[str]:
        path = self._stderr_path(server_id)
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()[
                -lines:
            ]
        except OSError:
            return []
        return [Redactor.redact(line)[:300] for line in content]

    def state(self, server_id: str) -> MCPServerState:
        return self.states.setdefault(server_id, MCPServerState(server_id=server_id))

    def is_connected(self, server_id: str) -> bool:
        client = self._clients.get(server_id)
        if client is None:
            return False
        active = client._is_session_active()
        if not active and self.state(server_id).status == "connected":
            st = self.state(server_id)
            st.status = "error"
            st.last_error = "The connection was closed by the server."
        return active

    def connected_ids(self) -> list[str]:
        return [sid for sid in list(self._clients) if self.is_connected(sid)]

    def tools(self, server_id: str) -> list[tuple[SafeMCPAgentTool, MCPToolInfo]]:
        return (
            list(self._tools.get(server_id, [])) if self.is_connected(server_id) else []
        )

    # ------------------------------------------------------------------ lifecycle
    def connect(
        self, cfg: MCPServerConfig, default_enabled: bool = True
    ) -> MCPServerState:
        with self._lock:
            self.disconnect(cfg.id)
            st = self.state(cfg.id)
            st.status, st.last_attempt_at, st.last_error = (
                "connecting",
                datetime.now(),
                None,
            )
            client: MCPClient | None = None
            try:
                client = build_client(
                    cfg, self._secrets, self._policy(), self._stderr_path(cfg.id)
                )
                client.start()
                tools = discover_tools(client, cfg, default_enabled=default_enabled)
            except Exception as exc:  # noqa: BLE001 - recorded on the server state
                if client is not None:
                    self._safe_stop(client)
                st.status = "error"
                st.last_error = sanitize_error(exc)
                logger.warning(
                    "MCP server %s failed to connect: %s", cfg.id, st.last_error
                )
                return st
            self._clients[cfg.id] = client
            self._tools[cfg.id] = tools
            st.status, st.tool_count, st.last_connected_at = (
                "connected",
                len(tools),
                datetime.now(),
            )
            logger.info("MCP server %s connected with %d tools", cfg.id, len(tools))
            return st

    def refresh_tools(
        self, cfg: MCPServerConfig, default_enabled: bool = True
    ) -> MCPServerState:
        with self._lock:
            client = self._clients.get(cfg.id)
            st = self.state(cfg.id)
            if client is None or not self.is_connected(cfg.id):
                st.status, st.last_error = "error", "Server is not connected."
                return st
            try:
                self._tools[cfg.id] = discover_tools(
                    client, cfg, default_enabled=default_enabled
                )
                st.tool_count, st.last_error = len(self._tools[cfg.id]), None
            except Exception as exc:  # noqa: BLE001
                st.last_error = sanitize_error(exc)
            return st

    def disconnect(self, server_id: str) -> None:
        with self._lock:
            client = self._clients.pop(server_id, None)
            self._tools.pop(server_id, None)
            if client is not None:
                self._safe_stop(client)
                st = self.state(server_id)
                st.status, st.tool_count = "disconnected", 0

    def disconnect_all(self) -> None:
        for server_id in list(self._clients):
            self.disconnect(server_id)

    def forget(self, server_id: str) -> None:
        self.disconnect(server_id)
        self.states.pop(server_id, None)

    @staticmethod
    def _safe_stop(client: MCPClient) -> None:
        try:
            client.stop(None, None, None)
        except Exception as exc:  # noqa: BLE001 - closing an already broken session
            logger.debug("Error while stopping MCP client: %s", sanitize_error(exc))
        errlog = getattr(client, "_docsgen_errlog", None)
        if errlog is not None:
            try:
                errlog.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ testing
    def test(self, cfg: MCPServerConfig) -> MCPTestResult:
        """Connect with a temporary client, initialize, list tools and disconnect."""
        started = time.monotonic()
        diagnostics: list[str] = [f"Transport: {cfg.transport.value}"]
        client: MCPClient | None = None
        stderr_path = self._log_dir / f"mcp_{cfg.id}.test.stderr.log"
        try:
            client = build_client(cfg, self._secrets, self._policy(), stderr_path)
            diagnostics.append("Configuration is valid.")
            client.start()
            diagnostics.append("MCP session initialized.")
            tools = discover_tools(client, cfg)
            names = [info.original_name for _, info in tools]
            diagnostics.append(f"Discovered {len(names)} tool(s).")
            return MCPTestResult(
                ok=True,
                message=f"Connection successful: {len(names)} tool(s) available.",
                tool_count=len(names),
                tool_names=names,
                latency_ms=int((time.monotonic() - started) * 1000),
                diagnostics=diagnostics,
            )
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"Error: {sanitize_error(exc)}")
            tail = []
            try:
                tail = [
                    Redactor.redact(x)[:300]
                    for x in stderr_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()[-15:]
                ]
            except OSError:
                pass
            if tail:
                diagnostics.append("Server stderr (last lines):")
                diagnostics.extend("  " + line for line in tail)
            return MCPTestResult(
                ok=False,
                message=f"Connection failed: {sanitize_error(exc, 300)}",
                latency_ms=int((time.monotonic() - started) * 1000),
                diagnostics=diagnostics,
            )
        finally:
            if client is not None:
                self._safe_stop(client)

    def call_tool(
        self, server_id: str, original_name: str, arguments: dict, timeout_s: int
    ) -> tuple[str, str]:
        """Invoke a tool directly (used by the Test Tool action). Returns (status, text)."""
        from datetime import timedelta

        client = self._clients.get(server_id)
        if client is None or not self.is_connected(server_id):
            raise RuntimeError("Server is not connected.")
        result = client.call_tool_sync(
            tool_use_id=f"test-{int(time.time() * 1000)}",
            name=original_name,
            arguments=arguments,
            read_timeout_seconds=timedelta(seconds=timeout_s),
        )
        texts = []
        for item in result.get("content", []):
            if "text" in item:
                texts.append(item["text"])
            elif "json" in item:
                texts.append(str(item["json"]))
            else:
                texts.append(f"[{next(iter(item.keys()), 'content')}]")
        return result.get("status", "error"), "\n".join(texts)
