"""AI Instructions page: task type, targets, custom instructions, research and tool approvals."""

from __future__ import annotations

from typing import Any

import streamlit as st

from document_engine.section_mapper import fillable_sections
from models.agent_models import AGENT_LABELS, TASK_LABELS, AgentRole, TaskType
from services.container import ServiceContainer
from ui.components import flash, goto, page_header, show_flashes

DEFAULT_INSTRUCTIONS = (
    "Analyze all uploaded documents and populate the uploaded Word template. Extract"
    " relevant facts, map information to appropriate sections, preserve the original"
    " formatting, and identify missing information. Do not fabricate any information."
)

TARGETED_TASKS = {
    TaskType.FILL_SELECTED,
    TaskType.REWRITE,
    TaskType.IMPROVE_QUALITY,
    TaskType.SUMMARIZE_SOURCES,
}


def render(c: ServiceContainer) -> None:
    page_header(
        "AI Instructions",
        "Tell the agents what to do. Uploaded documents can never override these"
        " instructions or the security rules.",
        "✍️",
    )
    show_flashes()
    cfg = c.session.run_config
    tasks = list(TaskType)
    task = st.selectbox(
        "Task",
        tasks,
        index=tasks.index(
            TaskType(cfg.get("task_type", TaskType.GENERATE_COMPLETE.value))
        ),
        format_func=lambda t: TASK_LABELS[t],
        key="instr_task",
    )
    instructions = st.text_area(
        "Instructions",
        value=c.session.instructions or DEFAULT_INSTRUCTIONS,
        height=220,
        key="instr_text",
        max_chars=8000,
    )

    targets: list[str] = []
    structure = c.session.template.structure if c.session.template else None
    if task in TARGETED_TASKS:
        if structure is None:
            st.warning("Upload a template to choose sections and placeholders.")
        else:
            options = [s.section_id for s in fillable_sections(structure)] + [
                f"{{{{{p.key}}}}}" for p in structure.placeholders
            ]
            labels = {
                s.section_id: f"{s.section_id} · {s.title}" for s in structure.sections
            } | {f"{{{{{p.key}}}}}": f"{{{{{p.key}}}}}" for p in structure.placeholders}
            help_text = (
                "Sections to write the summary into (leave empty to just produce a"
                " summary)."
                if task == TaskType.SUMMARIZE_SOURCES
                else "Targets to process (empty = all applicable)."
            )
            targets = st.multiselect(
                "Sections and placeholders",
                options,
                default=[t for t in cfg.get("selected_targets", []) if t in options],
                format_func=lambda o: labels.get(o, o),
                key="instr_targets",
                help=help_text,
            )
    elif task == TaskType.COMPARE_DOCUMENTS:
        docx_sources = [d for d in c.session.sources.values() if d.file_type == "docx"]
        if not docx_sources:
            st.warning(
                "Upload at least one DOCX source document to compare with the template."
            )
        targets = st.multiselect(
            "Documents to compare with the template",
            [d.doc_id for d in docx_sources],
            format_func=lambda i: f"{i} · {c.session.sources[i].file_name}",
            key="instr_compare",
        )

    st.markdown("#### Research (optional)")
    research_tools = [
        d.name
        for d in c.agents.agent_tools(AgentRole.RESEARCH_AGENT, c.document_context())
        if d.source == "mcp"
    ]
    research = st.toggle(
        "Allow the Research Agent to perform external research",
        value=cfg.get("research_enabled", False),
        key="instr_research",
        help=(
            "Uses only MCP tools you authorized for the Research Agent on the Tool"
            " Registry page."
        ),
    )
    topics = ""
    if research:
        if research_tools:
            st.caption("Research tools available: " + ", ".join(research_tools))
        else:
            st.warning(
                "No MCP tools are authorized for the Research Agent. Connect a server"
                " and authorize its tools on the Tool Registry page; otherwise research"
                " is limited to the uploaded sources."
            )
        topics = st.text_area(
            "Research topics or questions",
            value=cfg.get("research_topics", ""),
            key="instr_topics",
            placeholder=(
                "e.g. Verify the client's headquarters and number of employees."
            ),
        )

    st.markdown("#### Approvals for consequential tools")
    ctx = c.document_context()
    consequential: dict[str, tuple[Any, list[str]]] = {}
    for role in AgentRole:
        for d in c.agents.agent_tools(role, ctx):
            if d.requires_approval:
                consequential.setdefault(d.name, (d, []))[1].append(AGENT_LABELS[role])
    approved: list[str] = []
    if consequential:
        st.warning(
            "These authorized tools may change data or perform external actions. They"
            " are blocked during the run unless you approve them here."
        )
        approved = st.multiselect(
            "Approve for this run",
            list(consequential),
            default=[t for t in cfg.get("approved_tools", []) if t in consequential],
            format_func=lambda n: f"{n} — used by {', '.join(consequential[n][1])}",
            key="instr_approvals",
        )
    else:
        st.caption("No consequential tools are authorized for any agent.")

    b = st.columns([1, 1, 3])
    save = b[0].button("Save instructions", width="stretch")
    start = b[1].button("Save & open workflow →", type="primary", width="stretch")
    if save or start:
        c.session.instructions = instructions
        c.session.run_config = {
            "task_type": task.value,
            "selected_targets": targets,
            "research_enabled": research,
            "research_topics": topics,
            "approved_tools": approved,
        }
        flash("Instructions saved. They apply to the next workflow run.")
        if start:
            goto("Agent Workflow")
        st.rerun()
