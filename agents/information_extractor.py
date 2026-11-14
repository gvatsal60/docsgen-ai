"""Agent 2 - Information Extraction: facts, relationships, mappings, conflicts and gaps, with source references."""

from __future__ import annotations

from agents.base import GROUNDING_RULES, AgentSpec
from agents.prompting import (
    compact_json,
    sources_digest,
    task_block,
    template_digest,
    user_answers_block,
)
from models.agent_models import AgentRole, ExtractionResult, TaskType, TemplateAnalysis
from tools.context import DocumentContext

SPEC = AgentSpec(
    role=AgentRole.INFORMATION_EXTRACTOR,
    description=(
        "Extracts facts, dates, names and numbers from the sources, maps them to"
        " template sections and placeholders, and reports conflicts and missing"
        " information."
    ),
    output_model=ExtractionResult,
    system_prompt=f"""\
You are the Information Extraction agent in a document automation system.

Work method:
1. Read the source chunks provided. Use the source tools to read omitted chunks, tables and spreadsheets.
2. Extract atomic facts relevant to the template requirements: names, organizations, dates, amounts,
   quantities, requirements, scope items, risks. Give each fact an id (F1, F2, ...), the exact value,
   and the chunk ids it came from in source_chunk_ids. Leave the 'sources' field empty.
3. Map facts to the template targets (section ids or placeholder keys) they support.
4. Record relationships between facts (for example "budget line items sum to the total").
5. Report conflicts when sources disagree (cite both facts).
6. Report missing information for each target that cannot be filled from the sources, marking it
   critical when the document would be wrong or incomplete without it, and write a precise question.

{GROUNDING_RULES}
""",
)


def build_prompt(
    ctx: DocumentContext,
    analysis: TemplateAnalysis | None,
    task: TaskType,
    instructions: str,
    answers: dict[str, str],
    budget: int,
) -> str:
    parts = [
        task_block(task, instructions),
        "TEMPLATE OUTLINE:",
        template_digest(ctx, 8_000),
    ]
    if analysis is not None:
        parts += [
            "TEMPLATE REQUIREMENTS (from the Document Analyzer):",
            compact_json(analysis, 12_000),
        ]
    if answers:
        parts.append(user_answers_block(answers))
    parts += [
        "SOURCE DOCUMENTS:",
        sources_digest(ctx, budget),
        "Return the ExtractionResult.",
    ]
    return "\n\n".join(parts)
