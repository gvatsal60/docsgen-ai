"""Discover tools from a connected MCP server and classify their risk.

Tool descriptions and annotations come from external servers and are treated as untrusted hints:
they are length-limited before reaching the model, and "read-only" claims never bypass the user's
explicit enable/permission choices.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

from models.mcp_models import MCPServerConfig, MCPToolInfo

MAX_DESCRIPTION_CHARS = 1_000
MAX_PAGES = 50

_CONSEQUENTIAL = re.compile(
    r"(^|_|-)(delete|remove|drop|truncate|destroy|purge|kill|terminate|shutdown|reboot|write|update|upsert|insert|"
    r"create|put|post|patch|send|email|publish|deploy|execute|exec|run|shell|command|move|rename|transfer|pay|"
    r"charge|order|book|commit|push|merge|revoke|grant|set)($|_|-)",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_description(text: str | None) -> str:
    text = _CONTROL.sub("", text or "").strip()
    return (
        text
        if len(text) <= MAX_DESCRIPTION_CHARS
        else text[: MAX_DESCRIPTION_CHARS - 1] + "…"
    )


class SafeMCPAgentTool(MCPAgentTool):
    """MCP tool adapter that limits the (untrusted) description length sent to the model."""

    @property
    def tool_spec(self):
        spec = dict(super().tool_spec)
        spec["description"] = clean_description(spec.get("description"))
        return spec  # type: ignore[return-value]


def _annotations(spec: dict[str, Any]) -> dict[str, Any]:
    return dict(spec.get("annotations") or {})


def classify(
    original_name: str, annotations: dict[str, Any]
) -> tuple[bool | None, bool | None, bool | None, bool, str | None]:
    """Return (read_only, destructive, open_world, requires_approval, reason)."""
    read_only = annotations.get("readOnlyHint")
    destructive = annotations.get("destructiveHint")
    open_world = annotations.get("openWorldHint")
    if destructive is True:
        return (
            read_only,
            destructive,
            open_world,
            True,
            "The server marks this tool as destructive.",
        )
    if read_only is True:
        return read_only, destructive, open_world, False, None
    if _CONSEQUENTIAL.search(original_name):
        return (
            read_only,
            destructive,
            open_world,
            True,
            "The tool name suggests it changes data or performs an external action.",
        )
    if read_only is False:
        return (
            read_only,
            destructive,
            open_world,
            True,
            "The server reports that this tool is not read-only.",
        )
    return read_only, destructive, open_world, False, None


def discover_tools(
    client: MCPClient, cfg: MCPServerConfig, default_enabled: bool = True
) -> list[tuple[SafeMCPAgentTool, MCPToolInfo]]:
    """List all tools (following pagination) and wrap them with execution timeouts."""
    discovered: list[tuple[SafeMCPAgentTool, MCPToolInfo]] = []
    token: str | None = None
    timeout = timedelta(seconds=cfg.tool_timeout)
    prefix = cfg.effective_prefix + "_"
    for _ in range(MAX_PAGES):
        page = client.list_tools_sync(pagination_token=token)
        for listed in page:
            tool = SafeMCPAgentTool(
                listed.mcp_tool, client, name_override=listed.tool_name, timeout=timeout
            )
            spec = tool.tool_spec
            original = listed.mcp_tool.name
            ann = _annotations(spec)
            read_only, destructive, open_world, needs_approval, reason = classify(
                original, ann
            )
            enabled = cfg.tool_states.get(original)
            if enabled is None:
                # New tools: enabled by default only when they are not consequential.
                enabled = default_enabled and not needs_approval
            info = MCPToolInfo(
                qualified_name=(
                    listed.tool_name
                    if listed.tool_name.startswith(prefix)
                    else cfg.qualified_tool_name(original)
                ),
                original_name=original,
                server_id=cfg.id,
                server_name=cfg.name,
                title=getattr(listed.mcp_tool, "title", None),
                description=clean_description(spec.get("description")),
                input_schema=dict((spec.get("inputSchema") or {}).get("json") or {}),
                enabled=bool(enabled),
                read_only=read_only,
                destructive=destructive,
                open_world=open_world,
                requires_approval=needs_approval,
                approval_reason=reason,
            )
            discovered.append((tool, info))
        token = getattr(page, "pagination_token", None)
        if not token:
            break
    return discovered
