"""Agent 4 - Document Generation: writes section content and placeholder values from extracted facts."""

from __future__ import annotations

from agents.base import GROUNDING_RULES, AgentSpec
from agents.prompting import (
    compact_json,
    sources_digest,
    task_block,
    template_digest,
    user_answers_block,
)
from models.agent_models import (
    AgentRole,
    ExtractionResult,
    GenerationResult,
    ResearchResult,
    TaskType,
    TemplateAnalysis,
)
from tools.context import DocumentContext

SPEC = AgentSpec(
    role=AgentRole.DOCUMENT_GENERATOR,
    description=(
        "Generates section content and placeholder values that follow the template's"
        " style, using only extracted facts."
    ),
    output_model=GenerationResult,
    system_prompt=f"""\
You are the Document Generation agent in a document automation system.

Work method:
1. Produce one GeneratedSection per requested target, using the exact target ids/keys.
   target_type is "section" for ids like s04 and "placeholder" for placeholder keys.
2. Sections: write well-structured prose in the template's style and terminology. Separate paragraphs
   with a blank line; use "- " for bullet items and "1. " for numbered items; **bold** sparingly; use a
   pipe table (| a | b | with a |---| separator row) only when tabular data is clearly appropriate.
   Do not repeat the section heading.
3. Placeholders: return only the value, without surrounding text.
4. List the fact ids you used. Put any statement you could not support in unsupported_claims and do not
   include it in the content. Put information that must come from the user in needs_user_input.
5. Do not fill a target if there is no supporting information; return an empty content string and
   explain in needs_user_input.
6. Verified research findings may be used and must be attributed; unverified findings must not be stated as fact.

{GROUNDING_RULES}
""",
)


def build_prompt(
    ctx: DocumentContext,
    task: TaskType,
    instructions: str,
    targets: list[str],
    analysis: TemplateAnalysis | None,
    extraction: ExtractionResult | None,
    research: ResearchResult | None,
    answers: dict[str, str],
    budget: int,
    existing_content: dict[str, str] | None = None,
) -> str:
    parts = [
        task_block(task, instructions, targets),
        "TEMPLATE OUTLINE:",
        template_digest(ctx, 8_000),
    ]
    if analysis is not None:
        parts += ["TEMPLATE REQUIREMENTS:", compact_json(analysis, 10_000)]
    if extraction is not None:
        parts += ["EXTRACTED FACTS AND MAPPINGS:", compact_json(extraction, 20_000)]
    if research is not None and research.performed:
        parts += ["RESEARCH FINDINGS:", compact_json(research, 6_000)]
    if answers:
        parts.append(user_answers_block(answers))
    if existing_content:
        parts.append(
            "EXISTING CONTENT TO REWRITE OR IMPROVE (keep all facts, do not add new"
            " ones):"
        )
        parts += [f"[{k}]\n{v[:6000]}" for k, v in existing_content.items()]
    parts += [
        "SOURCE EXCERPTS (for wording and verification):",
        sources_digest(ctx, budget // 2),
    ]
    parts.append(
        f"Generate content for exactly these targets: {', '.join(targets)}. Return the"
        " GenerationResult."
    )
    return "\n\n".join(parts)
