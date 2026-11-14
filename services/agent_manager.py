"""Strands Agent Manager: combines the selected model with the tools authorized for each agent.

Separation of concerns:

* :class:`services.llm_service.LLMService` decides *which model* (provider, model, credentials).
* :class:`services.mcp_manager.MCPManager` decides *which external tools exist* (servers, discovery).
* :class:`tools.registry.ToolRegistry` decides *which tools each agent may use*.
* This manager builds a fresh Strands ``Agent`` per run from those three, so provider, model, MCP
  server or permission changes take effect on the next agent run without restarting the app.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from strands import Agent

from agents import AGENT_SPECS
from models.agent_models import AgentRole, AgentRunRecord
from services.llm_service import LLMService
from services.security import SECURITY_PREAMBLE, sanitize_error
from tools.context import DocumentContext
from tools.registry import ToolDescriptor, ToolRegistry

logger = logging.getLogger(__name__)

MAX_TURNS = {
    AgentRole.DOCUMENT_ANALYZER: 12,
    AgentRole.INFORMATION_EXTRACTOR: 30,
    AgentRole.RESEARCH_AGENT: 20,
    AgentRole.DOCUMENT_GENERATOR: 25,
    AgentRole.DOCUMENT_VALIDATOR: 20,
    AgentRole.QUALITY_REVIEWER: 12,
}


class AgentRunError(RuntimeError):
    def __init__(self, message: str, record: AgentRunRecord) -> None:
        super().__init__(message)
        self.record = record


class AgentManager:
    def __init__(
        self,
        llm: LLMService,
        registry: ToolRegistry,
        timeout_provider: Callable[[], int] = lambda: 300,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self._timeout_provider = timeout_provider

    def agent_tools(
        self, role: AgentRole, ctx: DocumentContext
    ) -> list[ToolDescriptor]:
        _, descriptors = self.registry.tools_for_agent(role, ctx)
        return descriptors

    def build_agent(
        self, role: AgentRole, ctx: DocumentContext, approved_tools: set[str]
    ) -> tuple[Agent, Any, list[ToolDescriptor]]:
        spec = AGENT_SPECS[role]
        model = self.llm.get_model()  # always the currently applied provider/model
        tools, descriptors = self.registry.tools_for_agent(role, ctx)
        guard = self.registry.guard(
            role,
            descriptors,
            approved_tools,
            extra_allowed={spec.output_model.__name__},
            max_result_chars=ctx.max_result_chars,
        )
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=SECURITY_PREAMBLE + "\n" + spec.system_prompt,
            callback_handler=None,
            hooks=[guard],
            name=spec.label,
            description=spec.description,
        )
        return agent, guard, descriptors

    def run(
        self,
        role: AgentRole,
        prompt: str,
        ctx: DocumentContext,
        approved_tools: set[str] | None = None,
    ) -> tuple[BaseModel, AgentRunRecord]:
        """Run an agent and return its validated structured output. Raises AgentRunError on failure."""
        spec = AGENT_SPECS[role]
        started = time.monotonic()
        record = AgentRunRecord(
            role=role, ok=False, duration_ms=0, model_label=self.llm.describe()
        )
        try:
            agent, guard, descriptors = self.build_agent(
                role, ctx, approved_tools or set()
            )
        except Exception as exc:
            record.error = sanitize_error(exc)
            raise AgentRunError(record.error, record) from exc
        record.tools_available = [d.name for d in descriptors]

        cancel = threading.Event()

        def _invoke():
            return agent(
                prompt,
                structured_output_model=spec.output_model,
                limits={"turns": MAX_TURNS[role], "structured_output_attempts": 3},
                cancel_signal=cancel,
            )

        timeout = self._timeout_provider()
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"agent-{role.value}"
        )
        try:
            result = executor.submit(_invoke).result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            cancel.set()
            record.error = f"{spec.label} did not finish within {timeout} seconds."
            raise AgentRunError(record.error, record) from exc
        except Exception as exc:
            record.error = sanitize_error(exc)
            raise AgentRunError(f"{spec.label} failed: {record.error}", record) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            record.duration_ms = int((time.monotonic() - started) * 1000)
            record.tool_calls = list(guard.calls)

        output = getattr(result, "structured_output", None)
        if output is None:
            record.error = (
                f"{spec.label} did not return structured output (stop reason:"
                f" {getattr(result, 'stop_reason', 'unknown')})."
            )
            raise AgentRunError(record.error, record)
        record.ok = True
        logger.info(
            "%s finished in %d ms using %d tool call(s)",
            spec.label,
            record.duration_ms,
            len(record.tool_calls),
        )
        return output, record
