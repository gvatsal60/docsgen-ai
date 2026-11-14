"""Centralized tool registry for built-in Python tools and external MCP tools.

Responsibilities: registration, discovery, filtering, enable/disable, agent access control,
execution (direct tests), execution logging and approval enforcement.

A tool is given to an agent only when *all* of these hold:

1. Built-in: the tool is enabled. MCP: its server is enabled and connected and the tool is enabled.
2. The agent is authorized for the tool in the agent-to-tool permission mapping.

At run time a Strands hook re-checks authorization for every call (defense in depth), blocks
consequential tools that the user has not approved for the run, and logs each execution.
"""

from __future__ import annotations

import collections
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from strands.hooks import (
    AfterToolCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from models.agent_models import AgentRole
from services.security import sanitize_error, wrap_untrusted
from storage.configuration_store import ConfigurationStore
from tools import (
    csv_tools,
    docx_tools,
    excel_tools,
    pdf_tools,
    text_extraction,
    validation_tools,
)
from tools.context import DocumentContext

A = AgentRole


@dataclass(frozen=True)
class BuiltinToolDef:
    name: str
    category: str
    description: str
    factory: Callable[[DocumentContext], Any]
    default_agents: frozenset[AgentRole] = field(default_factory=frozenset)


def _d(
    name: str, category: str, description: str, factory: Callable, *agents: AgentRole
) -> BuiltinToolDef:
    return BuiltinToolDef(name, category, description, factory, frozenset(agents))


BUILTIN_TOOLS: list[BuiltinToolDef] = [
    _d(
        "get_template_outline",
        "DOCX",
        "Template section outline and placeholders.",
        docx_tools.make_get_template_outline,
        A.DOCUMENT_ANALYZER,
        A.DOCUMENT_GENERATOR,
    ),
    _d(
        "get_template_section",
        "DOCX",
        "Details of one template section.",
        docx_tools.make_get_template_section,
        A.DOCUMENT_ANALYZER,
        A.INFORMATION_EXTRACTOR,
        A.DOCUMENT_GENERATOR,
    ),
    _d(
        "list_template_placeholders",
        "DOCX",
        "Placeholders and their locations.",
        docx_tools.make_list_template_placeholders,
        A.DOCUMENT_ANALYZER,
        A.INFORMATION_EXTRACTOR,
    ),
    _d(
        "get_document_tables",
        "DOCX",
        "Tables extracted from Word/PDF sources.",
        docx_tools.make_get_document_tables,
        A.INFORMATION_EXTRACTOR,
        A.DOCUMENT_VALIDATOR,
    ),
    _d(
        "get_pdf_page",
        "PDF",
        "Text of one PDF page.",
        pdf_tools.make_get_pdf_page,
        A.INFORMATION_EXTRACTOR,
        A.DOCUMENT_VALIDATOR,
    ),
    _d(
        "get_spreadsheet_sheet",
        "Excel",
        "Rows of an Excel sheet or CSV table.",
        excel_tools.make_get_spreadsheet_sheet,
        A.INFORMATION_EXTRACTOR,
        A.DOCUMENT_VALIDATOR,
    ),
    _d(
        "spreadsheet_statistics",
        "Excel",
        "Numeric column statistics for sheets.",
        excel_tools.make_spreadsheet_statistics,
        A.INFORMATION_EXTRACTOR,
        A.DOCUMENT_VALIDATOR,
    ),
    _d(
        "get_csv_rows",
        "CSV",
        "Rows of a CSV file with optional filtering.",
        csv_tools.make_get_csv_rows,
        A.INFORMATION_EXTRACTOR,
    ),
    _d(
        "csv_statistics",
        "CSV",
        "Numeric column statistics for CSV files.",
        csv_tools.make_csv_statistics,
        A.INFORMATION_EXTRACTOR,
        A.DOCUMENT_VALIDATOR,
    ),
    _d(
        "list_source_documents",
        "Text",
        "List uploaded source documents.",
        text_extraction.make_list_source_documents,
        A.INFORMATION_EXTRACTOR,
        A.RESEARCH_AGENT,
        A.DOCUMENT_VALIDATOR,
    ),
    _d(
        "read_source_document",
        "Text",
        "Read the text of a source document.",
        text_extraction.make_read_source_document,
        A.INFORMATION_EXTRACTOR,
        A.DOCUMENT_VALIDATOR,
    ),
    _d(
        "search_source_documents",
        "Text",
        "Keyword search across sources.",
        text_extraction.make_search_source_documents,
        A.INFORMATION_EXTRACTOR,
        A.RESEARCH_AGENT,
        A.DOCUMENT_GENERATOR,
        A.DOCUMENT_VALIDATOR,
    ),
    _d(
        "get_source_chunk",
        "Text",
        "Full text of a source chunk by id.",
        text_extraction.make_get_source_chunk,
        A.INFORMATION_EXTRACTOR,
        A.RESEARCH_AGENT,
        A.DOCUMENT_GENERATOR,
        A.DOCUMENT_VALIDATOR,
    ),
    _d(
        "get_source_entities",
        "Text",
        "Dates, amounts, names found by pattern matching.",
        text_extraction.make_get_source_entities,
        A.INFORMATION_EXTRACTOR,
    ),
    _d(
        "verify_values_in_sources",
        "Validation",
        "Check numbers/dates against sources.",
        validation_tools.make_verify_values_in_sources,
        A.DOCUMENT_VALIDATOR,
        A.QUALITY_REVIEWER,
    ),
    _d(
        "check_remaining_placeholders",
        "Validation",
        "Find unfilled placeholders and gap markers.",
        validation_tools.make_check_placeholders,
        A.DOCUMENT_VALIDATOR,
        A.QUALITY_REVIEWER,
    ),
    _d(
        "find_duplicate_content",
        "Validation",
        "Find sentences repeated across sections.",
        validation_tools.make_find_duplicate_content,
        A.DOCUMENT_VALIDATOR,
        A.QUALITY_REVIEWER,
    ),
    _d(
        "compare_texts",
        "Comparison",
        "Similarity and diff of two texts.",
        validation_tools.make_compare_texts,
        A.QUALITY_REVIEWER,
    ),
    _d(
        "readability_stats",
        "Review",
        "Readability statistics for a text.",
        validation_tools.make_readability_stats,
        A.QUALITY_REVIEWER,
    ),
]
BUILTIN_BY_NAME = {d.name: d for d in BUILTIN_TOOLS}


class ToolDescriptor(BaseModel):
    name: str
    source: Literal["builtin", "mcp"]
    category: str
    description: str
    enabled: bool
    available: bool  # MCP: server connected
    server_id: str | None = None
    server_name: str | None = None
    original_name: str | None = None
    requires_approval: bool = False
    approval_reason: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolExecution(BaseModel):
    at: datetime = Field(default_factory=datetime.now)
    agent: str
    tool: str
    source: str
    status: Literal["success", "error", "blocked"]
    duration_ms: int | None = None
    detail: str = ""


class ToolRegistry:
    def __init__(self, config_store: ConfigurationStore, mcp_manager) -> None:
        self._store = config_store
        self._mcp = mcp_manager
        self._lock = threading.Lock()
        self.executions: collections.deque[ToolExecution] = collections.deque(
            maxlen=500
        )

    # ------------------------------------------------------------------ built-in state
    def builtin_states(self) -> dict[str, bool]:
        saved = self._store.get("builtin_tool_states", {}) or {}
        return {d.name: bool(saved.get(d.name, True)) for d in BUILTIN_TOOLS}

    def set_builtin_enabled(self, names: list[str], enabled: bool) -> None:
        states = self.builtin_states()
        for name in names:
            if name in BUILTIN_BY_NAME:
                states[name] = enabled
        self._store.update("builtin_tool_states", states)

    # ------------------------------------------------------------------ discovery
    def descriptors(self) -> list[ToolDescriptor]:
        states = self.builtin_states()
        result = [
            ToolDescriptor(
                name=d.name,
                source="builtin",
                category=d.category,
                description=d.description,
                enabled=states[d.name],
                available=True,
            )
            for d in BUILTIN_TOOLS
        ]
        for info in self._mcp.tool_infos():
            cfg = self._mcp.get_server(info.server_id)
            result.append(
                ToolDescriptor(
                    name=info.qualified_name,
                    source="mcp",
                    category=f"MCP · {info.server_name}",
                    description=info.description,
                    enabled=bool(info.enabled and cfg is not None and cfg.enabled),
                    available=True,
                    server_id=info.server_id,
                    server_name=info.server_name,
                    original_name=info.original_name,
                    requires_approval=info.requires_approval,
                    approval_reason=info.approval_reason,
                    input_schema=info.input_schema,
                )
            )
        return result

    def filter(
        self,
        descriptors: list[ToolDescriptor],
        *,
        query: str = "",
        source: str = "all",
        server_id: str | None = None,
        only_enabled: bool = False,
    ) -> list[ToolDescriptor]:
        q = query.lower().strip()
        out = []
        for d in descriptors:
            if source != "all" and d.source != source:
                continue
            if server_id and d.server_id != server_id:
                continue
            if only_enabled and not d.enabled:
                continue
            if q and q not in d.name.lower() and q not in d.description.lower():
                continue
            out.append(d)
        return out

    # ------------------------------------------------------------------ permissions
    def default_permissions(self) -> dict[AgentRole, set[str]]:
        perms: dict[AgentRole, set[str]] = {role: set() for role in AgentRole}
        for d in BUILTIN_TOOLS:
            for role in d.default_agents:
                perms[role].add(d.name)
        return perms

    def permissions(self) -> dict[AgentRole, set[str]]:
        saved = self._store.get("agent_permissions")
        perms = self.default_permissions()
        if saved:
            for role in AgentRole:
                if role.value in saved:
                    perms[role] = set(saved[role.value])
        return perms

    def save_permissions(self, perms: dict[AgentRole, set[str]]) -> None:
        self._store.update(
            "agent_permissions",
            {role.value: sorted(names) for role, names in perms.items()},
        )

    def set_permission(self, role: AgentRole, tool_name: str, allowed: bool) -> None:
        perms = self.permissions()
        (perms[role].add if allowed else perms[role].discard)(tool_name)
        self.save_permissions(perms)

    def reset_permissions(self) -> None:
        self.save_permissions(self.default_permissions())

    def is_authorized(self, role: AgentRole, tool_name: str) -> bool:
        return tool_name in self.permissions().get(role, set())

    # ------------------------------------------------------------------ agent tools
    def tools_for_agent(
        self, role: AgentRole, ctx: DocumentContext
    ) -> tuple[list[Any], list[ToolDescriptor]]:
        """Return (Strands tool objects, descriptors) the agent may use right now."""
        allowed = self.permissions().get(role, set())
        states = self.builtin_states()
        tools: list[Any] = []
        descriptors: list[ToolDescriptor] = []
        for d in BUILTIN_TOOLS:
            if d.name in allowed and states.get(d.name, True):
                tools.append(d.factory(ctx))
                descriptors.append(
                    ToolDescriptor(
                        name=d.name,
                        source="builtin",
                        category=d.category,
                        description=d.description,
                        enabled=True,
                        available=True,
                    )
                )
        for name, (tool_obj, info) in self._mcp.available_tools().items():
            if name in allowed:
                tools.append(tool_obj)
                descriptors.append(
                    ToolDescriptor(
                        name=name,
                        source="mcp",
                        category=f"MCP · {info.server_name}",
                        description=info.description,
                        enabled=True,
                        available=True,
                        server_id=info.server_id,
                        server_name=info.server_name,
                        original_name=info.original_name,
                        requires_approval=info.requires_approval,
                        approval_reason=info.approval_reason,
                    )
                )
        return tools, descriptors

    def guard(
        self,
        role: AgentRole,
        descriptors: list[ToolDescriptor],
        approved_tools: set[str],
        extra_allowed: set[str] | None = None,
        max_result_chars: int = 12_000,
    ) -> ToolGuardHook:
        return ToolGuardHook(
            self,
            role,
            {d.name: d for d in descriptors},
            approved_tools,
            extra_allowed or set(),
            max_result_chars,
        )

    def record(self, execution: ToolExecution) -> None:
        with self._lock:
            self.executions.appendleft(execution)

    # ------------------------------------------------------------------ direct execution
    def execute_builtin(
        self, name: str, arguments: dict[str, Any], ctx: DocumentContext
    ) -> str:
        definition = BUILTIN_BY_NAME.get(name)
        if definition is None:
            raise KeyError(f"Unknown built-in tool: {name}")
        tool_obj = definition.factory(ctx)
        started = time.monotonic()
        try:
            output = tool_obj(**arguments)
            self.record(
                ToolExecution(
                    agent="user (test)",
                    tool=name,
                    source="builtin",
                    status="success",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
            return str(output)
        except Exception as exc:
            self.record(
                ToolExecution(
                    agent="user (test)",
                    tool=name,
                    source="builtin",
                    status="error",
                    detail=sanitize_error(exc),
                )
            )
            raise


class ToolGuardHook(HookProvider):
    """Strands hook: authorization re-check, approval enforcement and execution logging."""

    def __init__(
        self,
        registry: ToolRegistry,
        role: AgentRole,
        allowed: dict[str, ToolDescriptor],
        approved: set[str],
        extra_allowed: set[str],
        max_result_chars: int = 12_000,
    ) -> None:
        self.registry = registry
        self.max_result_chars = max_result_chars
        self.role = role
        self.allowed = allowed
        self.approved = approved
        self.extra_allowed = (
            extra_allowed  # e.g. the structured-output tool registered by Strands
        )
        self.calls: list[str] = []
        self._started: dict[str, float] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)

    def before(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use.get("name", "")
        if name in self.extra_allowed:
            return
        descriptor = self.allowed.get(name)
        if descriptor is None:
            event.cancel_tool = f"Tool '{name}' is not authorized for this agent."
            self.registry.record(
                ToolExecution(
                    agent=self.role.value,
                    tool=name,
                    source="unknown",
                    status="blocked",
                    detail="not authorized",
                )
            )
            return
        if descriptor.requires_approval and name not in self.approved:
            event.cancel_tool = (
                f"Tool '{name}' requires explicit user approval before it can run"
                f" ({descriptor.approval_reason or 'consequential operation'})."
                " Continue without it."
            )
            self.registry.record(
                ToolExecution(
                    agent=self.role.value,
                    tool=name,
                    source=descriptor.source,
                    status="blocked",
                    detail="approval required",
                )
            )
            return
        self._started[event.tool_use.get("toolUseId", name)] = time.monotonic()

    def after(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use.get("name", "")
        if name in self.extra_allowed:
            return
        tool_use_id = event.tool_use.get("toolUseId", name)
        started = self._started.pop(tool_use_id, None)
        if started is None:
            return  # cancelled in before()
        result = event.result or {}
        status = "success" if result.get("status") == "success" else "error"
        detail = ""
        if status == "error":
            texts = [
                c.get("text", "")
                for c in result.get("content", [])
                if isinstance(c, dict)
            ]
            detail = sanitize_error(" ".join(texts) or "tool error", 300)
        descriptor = self.allowed.get(name)
        if (
            descriptor is not None
            and descriptor.source == "mcp"
            and isinstance(event.result, dict)
        ):
            event.result = self._sanitize_mcp_result(name, event.result)
        self.calls.append(name)
        self.registry.record(
            ToolExecution(
                agent=self.role.value,
                tool=name,
                source=descriptor.source if descriptor else "unknown",
                status=status,  # type: ignore[arg-type]
                duration_ms=int((time.monotonic() - started) * 1000),
                detail=detail,
            )
        )

    def _sanitize_mcp_result(self, name: str, result: dict) -> dict:
        """Mark MCP output as untrusted data and cap its size before the model sees it.

        Text blocks are joined, truncated and wrapped in ``<untrusted_data>`` tags; structured JSON blocks are
        serialised into the same wrapper; anything else (images, documents) is dropped with a note, because
        DocsGen agents only work with text.
        """
        parts: list[str] = []
        dropped = 0
        for block in result.get("content", []) or []:
            if not isinstance(block, dict):
                dropped += 1
            elif "text" in block:
                parts.append(str(block.get("text", "")))
            elif "json" in block:
                try:
                    parts.append(
                        json.dumps(block["json"], ensure_ascii=False, default=str)
                    )
                except (TypeError, ValueError):
                    dropped += 1
            else:
                dropped += 1
        text = "\n".join(parts)
        if len(text) > self.max_result_chars:
            text = (
                text[: self.max_result_chars]
                + f"\n…[truncated; {len(text) - self.max_result_chars} more characters]"
            )
        if dropped:
            text += f"\n[{dropped} non-text content block(s) omitted]"
        sanitized = dict(result)
        sanitized["content"] = [{"text": wrap_untrusted(f"MCP tool {name}", text)}]
        return sanitized
