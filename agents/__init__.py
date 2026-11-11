"""The six specialized DocsGen agents (built with Strands Agents)."""

from typing import Any

from agents import (
    document_analyzer,
    document_generator,
    document_validator,
    information_extractor,
    quality_reviewer,
    research_agent,
)
from agents.base import AgentSpec
from models.agent_models import AgentRole

AGENT_SPECS: dict[AgentRole, AgentSpec[Any]] = {
    AgentRole.DOCUMENT_ANALYZER: document_analyzer.SPEC,
    AgentRole.INFORMATION_EXTRACTOR: information_extractor.SPEC,
    AgentRole.RESEARCH_AGENT: research_agent.SPEC,
    AgentRole.DOCUMENT_GENERATOR: document_generator.SPEC,
    AgentRole.DOCUMENT_VALIDATOR: document_validator.SPEC,
    AgentRole.QUALITY_REVIEWER: quality_reviewer.SPEC,
}

__all__ = ["AGENT_SPECS"]
