"""Shared definitions for the DocsGen agents.

Each agent module declares an :class:`AgentSpec`: its role, instructions, structured output model and
prompt builder. Agents never construct models or pick tools themselves; the
:class:`services.agent_manager.AgentManager` combines the model from the LLM service with the tools
the registry authorizes for that agent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel

from models.agent_models import AGENT_LABELS, AgentRole

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class AgentSpec(Generic[T]):
    role: AgentRole
    description: str
    system_prompt: str
    output_model: type[T]
    max_tool_calls_hint: int = 12

    @property
    def label(self) -> str:
        return AGENT_LABELS[self.role]


PromptBuilder = Callable[..., str]

GROUNDING_RULES = """\
GROUNDING RULES:
- Use only information from the provided sources, tool results that you retrieved, and the user's answers.
- Cite source chunk ids (for example D1-C3) for every fact you use.
- If something is not supported, list it as missing or unsupported instead of guessing.
- Keep names, numbers, dates and terminology exactly as they appear in the sources.
"""
