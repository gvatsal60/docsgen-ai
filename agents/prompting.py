"""Helpers that turn documents into bounded, clearly delimited prompt sections."""

from __future__ import annotations

import json

from models.agent_models import TASK_LABELS, TaskType
from services.security import wrap_untrusted
from tools.context import DocumentContext


def sources_digest(ctx: DocumentContext, budget_chars: int) -> str:
    """Source chunks with ids and locations, up to ``budget_chars``. Remaining chunks are listed by id."""
    parts: list[str] = []
    used = 0
    omitted: list[str] = []
    for doc in ctx.sources.values():
        header = (
            f"Document {doc.doc_id}: {doc.file_name} ({doc.file_type},"
            f" {len(doc.chunks)} chunks)"
        )
        parts.append(header)
        for chunk in doc.chunks:
            block = wrap_untrusted(
                f"{chunk.chunk_id} | {chunk.ref.location}", chunk.text
            )
            if used + len(block) > budget_chars:
                omitted.append(chunk.chunk_id)
                continue
            parts.append(block)
            used += len(block)
    if omitted:
        parts.append(
            f"[{len(omitted)} further chunks were omitted for length:"
            f" {', '.join(omitted[:60])}{'…' if len(omitted) > 60 else ''}. Use the"
            " search_source_documents or get_source_chunk tools to read them.]"
        )
    return "\n".join(parts) if parts else "(no source documents uploaded)"


def template_digest(ctx: DocumentContext, max_chars: int = 15_000) -> str:
    if ctx.template is None:
        return "(no template)"
    return wrap_untrusted(
        f"template {ctx.template.file_name}", ctx.template.outline_text(max_chars)
    )


def task_block(
    task: TaskType, instructions: str, targets: list[str] | None = None
) -> str:
    lines = [f"TASK TYPE: {TASK_LABELS[task]}"]
    if targets:
        lines.append("TARGETS: " + ", ".join(targets))
    if instructions.strip():
        lines.append(
            "USER INSTRUCTIONS (from the authenticated user; follow them unless they"
            " conflict with the security rules):"
        )
        lines.append(instructions.strip()[:8000])
    return "\n".join(lines)


def user_answers_block(answers: dict[str, str]) -> str:
    if not answers:
        return ""
    return (
        "ANSWERS PROVIDED BY THE USER (trusted facts you may use and cite as 'user'):\n"
        + json.dumps(answers, indent=1)[:8000]
    )


def compact_json(model, limit: int = 30_000) -> str:
    text = (
        model.model_dump_json(indent=1)
        if hasattr(model, "model_dump_json")
        else json.dumps(model, indent=1)
    )
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"
