"""Agent 1 - Document Analyzer: understands the template's structure and what each part needs."""

from __future__ import annotations

from agents.base import AgentSpec
from agents.prompting import task_block, template_digest
from models.agent_models import AgentRole, TaskType, TemplateAnalysis
from tools.context import DocumentContext

SPEC = AgentSpec(
    role=AgentRole.DOCUMENT_ANALYZER,
    description=(
        "Analyzes the DOCX template: sections, tables, placeholders and the information"
        " each part requires."
    ),
    output_model=TemplateAnalysis,
    system_prompt="""\
You are the Document Analyzer in a document automation system.
Your job: understand a Word template so other agents can fill it.

Work method:
1. Read the template outline provided (section ids such as s03, titles, current text, placeholders).
2. Use the template tools for details when needed.
3. For every section with a heading, describe its purpose and the specific information it needs.
   Mark should_fill=false for parts that must not be filled (cover pages, tables of contents,
   sections that already contain final text, signatures).
4. Describe every placeholder key and its expected type.
5. Note the writing style, tone and terminology the template implies.

Use the section ids and placeholder keys exactly as given. Do not invent sections.
""",
)


def build_prompt(ctx: DocumentContext, task: TaskType, instructions: str) -> str:
    return "\n\n".join(
        [
            task_block(task, instructions),
            "TEMPLATE OUTLINE:",
            template_digest(ctx),
            "Return the TemplateAnalysis.",
        ]
    )
