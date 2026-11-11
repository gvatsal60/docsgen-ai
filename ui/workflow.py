"""Agent Workflow page: readiness, per-agent tools, run the pipeline or single steps, results."""

from __future__ import annotations

from typing import Any

import streamlit as st

from models.agent_models import (
    AGENT_LABELS,
    STAGE_ORDER,
    TASK_LABELS,
    WRITING_TASKS,
    AgentRole,
    TaskType,
    WorkflowStage,
)
from services.container import ServiceContainer
from services.workflow_service import WorkflowService
from ui.components import badge, goto_button, page_header, run_with_status, show_flashes

STEP_LABELS = {
    WorkflowStage.NOT_STARTED: "Not started",
    WorkflowStage.TEMPLATE_ANALYZED: "Template analyzed",
    WorkflowStage.SOURCES_EXTRACTED: "Information extracted",
    WorkflowStage.RESEARCHED: "Research done",
    WorkflowStage.GENERATED: "Content generated",
    WorkflowStage.VALIDATED: "Validated",
    WorkflowStage.REVIEWED: "Quality reviewed",
    WorkflowStage.APPROVED: "Approved",
    WorkflowStage.DOCUMENT_GENERATED: "Document generated",
    WorkflowStage.COMPLETED: "Completed",
    WorkflowStage.FAILED: "Failed",
}


def _new_state(c: ServiceContainer):
    cfg = c.session.run_config
    return WorkflowService.new_state(
        TaskType(cfg.get("task_type", TaskType.GENERATE_COMPLETE.value)),
        c.session.instructions or "",
        selected_targets=[
            t.strip("{}") if t.startswith("{{") else t
            for t in cfg.get("selected_targets", [])
        ],
        research_enabled=cfg.get("research_enabled", False),
        research_topics=cfg.get("research_topics", ""),
        approved_tools=cfg.get("approved_tools", []),
    )


def render(c: ServiceContainer) -> None:
    page_header(
        "Agent Workflow",
        "Run the Strands agents. Every agent uses the AI provider currently applied and"
        " only the tools it is authorized to use.",
        "🤖",
    )
    show_flashes()
    ctx = c.document_context()
    cfg = c.session.run_config
    task = TaskType(cfg.get("task_type", TaskType.GENERATE_COMPLETE.value))

    ready_llm = c.llm.is_configured()
    ready_tpl = c.session.template is not None
    needs_sources = task not in (
        TaskType.IMPROVE_QUALITY,
        TaskType.VALIDATE_COMPLETENESS,
        TaskType.COMPARE_DOCUMENTS,
    )
    ready_src = bool(c.session.sources) or not needs_sources
    r = st.columns(4)
    r[0].markdown(
        "**AI provider** "
        f" \n{badge('Ready' if ready_llm else 'Missing', 'ok' if ready_llm else 'error')}"
        f" {c.llm.describe()}"
    )
    r[1].markdown(
        "**Template** "
        f" \n{badge('Ready' if ready_tpl else 'Missing', 'ok' if ready_tpl else 'error')}"
        f" {c.session.template.file_name if c.session.template else ''}"
    )
    sources_badge = badge(
        f"{len(c.session.sources)} file(s)", "ok" if ready_src else "error"
    )
    r[2].markdown(f"**Sources**  \n{sources_badge}")
    r[3].markdown(f"**Task**  \n{TASK_LABELS[task]}")
    if not ready_llm:
        goto_button(
            "Configure the AI provider", "AI Provider Configuration", key="wf_go_ai"
        )
    if not ready_tpl:
        goto_button("Upload a template", "Document Template Upload", key="wf_go_tpl")
    if needs_sources and not c.session.sources:
        goto_button("Upload source documents", "Source Documents", key="wf_go_src")

    with st.expander("Agents and their authorized tools"):
        cols = st.columns(3)
        for i, role in enumerate(AgentRole):
            names = [d.name for d in c.agents.agent_tools(role, ctx)]
            with cols[i % 3].container(border=True):
                st.markdown(f"**{AGENT_LABELS[role]}**")
                st.caption(", ".join(names) or "no tools")

    state = c.session.workflow
    can_run = ready_llm and ready_tpl and ready_src
    st.markdown("### Run")
    b = st.columns([2, 1, 1])
    if b[0].button(
        "▶ Run complete workflow", type="primary", disabled=not can_run, width="stretch"
    ):
        new_state = _new_state(c)
        c.session.workflow = new_state
        run_with_status(
            "Running agents",
            lambda progress: c.workflow.run_pipeline(
                new_state, c.document_context(), progress
            ),
        )
        st.rerun()
    if b[1].button("New run (clear results)", disabled=state is None, width="stretch"):
        c.session.workflow = None
        st.rerun()
    if state is not None and task in WRITING_TASKS and state.drafts:
        with b[2]:
            goto_button(
                "Review content →",
                "Document Review",
                key="wf_go_review",
                width="stretch",
            )

    with st.expander("Run individual steps"):
        steps = st.columns(6)
        step_defs = [
            (
                "1 · Analyze template",
                lambda s, p: c.workflow.analyze_template(s, c.document_context(), p),
            ),
            (
                "2 · Extract",
                lambda s, p: c.workflow.extract_information(s, c.document_context(), p),
            ),
            (
                "3 · Research",
                lambda s, p: c.workflow.research(s, c.document_context(), p),
            ),
            (
                "4 · Generate",
                lambda s, p: c.workflow.generate(s, c.document_context(), progress=p),
            ),
            (
                "5 · Validate",
                lambda s, p: c.workflow.validate(s, c.document_context(), p),
            ),
            (
                "6 · Quality review",
                lambda s, p: c.workflow.quality_review(s, c.document_context(), p),
            ),
        ]
        for col, (label, fn) in zip(steps, step_defs):
            if col.button(
                label, disabled=not can_run, width="stretch", key=f"step_{label}"
            ):
                if c.session.workflow is None:
                    c.session.workflow = _new_state(c)
                current = c.session.workflow

                def _run_step(
                    progress: Any, fn: Any = fn, current: Any = current
                ) -> Any:
                    return fn(current, progress)

                run_with_status(label, _run_step)
                st.rerun()

    state = c.session.workflow
    if state is None:
        st.info("No workflow run yet.")
        return

    st.markdown("### Progress")
    order = [s for s in STAGE_ORDER if s != WorkflowStage.NOT_STARTED]
    reached = (
        STAGE_ORDER.index(state.stage)
        if state.stage in STAGE_ORDER
        else (len(STAGE_ORDER) - 1 if state.stage == WorkflowStage.COMPLETED else 0)
    )
    st.progress(
        min(1.0, reached / (len(STAGE_ORDER) - 1)),
        text=f"{STEP_LABELS[state.stage]} · run {state.run_id}",
    )
    st.caption(
        " → ".join(
            (
                f"**{STEP_LABELS[s]}**"
                if STAGE_ORDER.index(s) <= reached
                else STEP_LABELS[s]
            )
            for s in order
        )
    )
    if state.last_error:
        st.error(state.last_error)

    _results(c, state)

    with st.expander("Event log", expanded=False):
        for e in reversed(state.events):
            icon = {"success": "✅", "warning": "⚠️", "error": "❌"}.get(e.level, "ℹ️")
            who = f" [{AGENT_LABELS[e.agent]}]" if e.agent else ""
            st.markdown(f"{icon} `{e.at:%H:%M:%S}` **{e.step}**{who} — {e.message}")
        if state.agent_runs:
            st.dataframe(
                [
                    {
                        "Agent": AGENT_LABELS[r.role],
                        "OK": r.ok,
                        "Seconds": round(r.duration_ms / 1000, 1),
                        "Tool calls": ", ".join(r.tool_calls),
                        "Model": r.model_label,
                        "Error": r.error or "",
                    }
                    for r in state.agent_runs
                ],
                hide_index=True,
                width="stretch",
            )


def _results(c: ServiceContainer, state) -> None:
    if state.template_analysis:
        with st.expander(
            f"Template analysis — {state.template_analysis.document_type}"
        ):
            st.write(state.template_analysis.summary)
            st.dataframe(
                [
                    {
                        "Section": s.section_id,
                        "Title": s.title,
                        "Fill": s.should_fill,
                        "Needs": "; ".join(s.required_information),
                    }
                    for s in state.template_analysis.sections
                ],
                hide_index=True,
                width="stretch",
            )
            if state.template_analysis.placeholders:
                st.dataframe(
                    [p.model_dump() for p in state.template_analysis.placeholders],
                    hide_index=True,
                    width="stretch",
                )
    if state.extraction:
        ex = state.extraction
        with st.expander(
            f"Extracted information — {len(ex.facts)} facts, {len(ex.missing)} gaps,"
            f" {len(ex.conflicts)} conflicts"
        ):
            st.dataframe(
                [
                    {
                        "ID": f.fact_id,
                        "Fact": f.statement,
                        "Value": f.value,
                        "Category": f.category,
                        "Confidence": f.confidence,
                        "Sources": "; ".join(s.label() for s in f.sources),
                    }
                    for f in ex.facts
                ],
                hide_index=True,
                width="stretch",
            )
            for conflict in ex.conflicts:
                st.warning(
                    f"Conflict: {conflict.description} ({', '.join(conflict.fact_ids)})"
                )
            for m in ex.missing:
                (st.error if m.critical else st.info)(
                    f"Missing for {m.target}: {m.description}"
                    + (f" — {m.question_for_user}" if m.question_for_user else "")
                )
    if state.research and (state.research.performed or state.research_enabled):
        with st.expander("Research"):
            for f in state.research.findings:
                st.markdown(
                    f"- {'✅ verified' if f.verified else '❔ unverified'} —"
                    f" {f.statement}"
                    + (
                        f" ([{f.citation_title or 'source'}]({f.citation_url}))"
                        if f.citation_url
                        else ""
                    )
                )
            for lim in state.research.limitations:
                st.caption(f"Limitation: {lim}")
    if state.validation:
        v = state.validation
        with st.expander(
            f"Validation — {v.completeness_score:.0f}% complete, {len(v.issues)}"
            " issue(s)"
        ):
            st.write(v.summary)
            st.dataframe(
                [i.model_dump() for i in v.issues], hide_index=True, width="stretch"
            )
    if state.quality:
        q = state.quality
        with st.expander(
            f"Quality review — score {q.overall_score:.0f}/100, {len(q.revisions)}"
            " suggestion(s)"
        ):
            st.write(q.summary)
            for label, text in (
                ("Grammar", q.grammar),
                ("Readability", q.readability),
                ("Consistency", q.consistency),
                ("Formatting", q.formatting),
            ):
                if text:
                    st.markdown(f"**{label}:** {text}")
    if state.summary_text:
        with st.expander("Source summary", expanded=True):
            st.markdown(state.summary_text)
    if state.comparison_text:
        with st.expander("Document comparison", expanded=True):
            st.markdown(state.comparison_text)
