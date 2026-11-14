"""Document Review page (human in the loop): edit, approve, reject, skip, regenerate, answer gaps."""

from __future__ import annotations

import difflib

import streamlit as st

from models.agent_models import DraftStatus, InsertMode, WorkflowStage
from services.container import ServiceContainer
from services.workflow_service import WorkflowService
from ui.components import (
    badge,
    flash,
    goto_button,
    page_header,
    run_with_status,
    show_flashes,
)

MODE_LABELS = {
    InsertMode.AUTO: "Auto (replace instructions / empty text, otherwise append)",
    InsertMode.REPLACE: "Replace the section's existing content",
    InsertMode.APPEND: "Append after existing content",
}


def render(c: ServiceContainer) -> None:
    page_header(
        "Document Review",
        "Nothing is written to your document until you approve it. Edit freely; every"
        " value is checked against the sources again before generation.",
        "🔍",
    )
    show_flashes()
    state = c.session.workflow
    if state is None or not state.drafts:
        st.info("There is no generated content to review yet.")
        goto_button("Open the agent workflow", "Agent Workflow", key="rv_go_wf")
        return
    structure = c.session.template.structure if c.session.template else None

    counts = {
        s: sum(1 for d in state.drafts.values() if d.status == s) for s in DraftStatus
    }
    m = st.columns(5)
    m[0].metric("Items", len(state.drafts))
    m[1].metric("Approved", counts[DraftStatus.APPROVED])
    m[2].metric("Pending", counts[DraftStatus.PENDING])
    m[3].metric("Rejected", counts[DraftStatus.REJECTED])
    m[4].metric("Skipped", counts[DraftStatus.SKIPPED])

    b = st.columns([1, 1, 1, 2])
    if b[0].button("✅ Approve all pending", type="primary", width="stretch"):
        n = WorkflowService.approve_all(state)
        flash(f"Approved {n} item(s).")
        st.rerun()
    if b[1].button("Reset all to pending", width="stretch"):
        for d in state.drafts.values():
            d.status = DraftStatus.PENDING
        st.rerun()
    if state.approved_drafts:
        with b[2]:
            goto_button(
                "Generate document →",
                "Document Generation",
                key="rv_go_gen",
                width="stretch",
            )

    # Missing information ---------------------------------------------------------------------
    missing = (
        sorted(state.extraction.missing, key=lambda m: not m.critical)
        if state.extraction
        else []
    )
    if missing:
        with st.expander(
            f"❓ Missing information — {sum(1 for m in missing if m.critical)}"
            f" critical, {len(missing)} total",
            expanded=any(m.critical for m in missing),
        ):
            st.caption(
                "Answer what you can. Answers are trusted facts: placeholder answers"
                " become values directly, and section answers are used when you"
                " regenerate."
            )
            with st.form("missing_form"):
                answers = {}
                for i, item in enumerate(missing):
                    question = item.question_for_user or item.description
                    key_q = f"{item.target}: {question}"[:200]
                    answers[(item.target, question)] = st.text_input(
                        f"{'🔴 ' if item.critical else ''}[{item.target}] {question}",
                        value=state.user_answers.get(key_q, ""),
                        key=f"miss_{i}",
                    )
                if st.form_submit_button("Save answers"):
                    for (target, question), answer in answers.items():
                        WorkflowService.record_answer(
                            state, target, question, answer, structure
                        )
                    flash("Answers saved. Regenerate affected sections to use them.")
                    st.rerun()

    if state.validation and state.validation.issues:
        errors = [i for i in state.validation.issues if i.severity == "error"]
        warnings = [i for i in state.validation.issues if i.severity == "warning"]
        st.markdown(
            f"**Validation:** {state.validation.completeness_score:.0f}% complete ·"
            f" {len(errors)} error(s) · {len(warnings)} warning(s)"
        )
    if state.extraction and state.extraction.conflicts:
        for conflict in state.extraction.conflicts:
            st.warning(f"Conflicting information: {conflict.description}")

    # Filters -------------------------------------------------------------------------------------
    f = st.columns([2, 2])
    status_filter = f[0].multiselect(
        "Show",
        list(DraftStatus),
        default=list(DraftStatus),
        format_func=lambda s: s.value.capitalize(),
        key="rv_filter",
    )
    type_filter = f[1].multiselect(
        "Type",
        ["section", "placeholder"],
        default=["section", "placeholder"],
        key="rv_type",
    )

    order = (
        {s.section_id: i for i, s in enumerate(structure.sections)} if structure else {}
    )
    drafts = sorted(
        state.drafts.values(),
        key=lambda d: (
            d.target_type != "placeholder",
            order.get(d.target, 999),
            d.target,
        ),
    )
    for draft in drafts:
        if draft.status not in status_filter or draft.target_type not in type_filter:
            continue
        _draft_card(c, state, draft)


def _draft_card(c: ServiceContainer, state, draft) -> None:
    key = f"draft_{draft.target}"
    label = (
        f"{draft.title}"
        if draft.target_type == "section"
        else f"{{{{{draft.target}}}}}"
    )
    flags = (
        (" ⚠️" if draft.warnings or draft.unsupported_claims else "")
        + (" ✏️" if draft.edited else "")
        + (" 💡" if draft.revision_suggestion else "")
    )
    with st.expander(
        f"{label} — {draft.status.value}{flags}",
        expanded=draft.status == DraftStatus.PENDING and bool(draft.warnings),
    ):
        st.markdown(
            badge(draft.status.value.capitalize(), draft.status.value)
            + f"  ·  `{draft.target}` ({draft.target_type})"
        )
        left, right = st.columns([3, 2], gap="large")
        with left:
            height = 80 if draft.target_type == "placeholder" else 260
            new_content = st.text_area(
                "Content", value=draft.content, height=height, key=f"{key}_content"
            )
            if new_content != draft.content:
                draft.history.append(draft.content)
                draft.content = new_content
                draft.edited = True
                if draft.status == DraftStatus.APPROVED:
                    draft.status = DraftStatus.PENDING
            if draft.target_type == "section":
                modes = list(InsertMode)
                draft.insert_mode = st.selectbox(
                    "Insertion",
                    modes,
                    index=modes.index(draft.insert_mode),
                    format_func=lambda m: MODE_LABELS[m],
                    key=f"{key}_mode",
                )
            if draft.original_content and draft.target_type == "section":
                with st.popover("Original template text"):
                    st.text(draft.original_content[:3000])
            b = st.columns(4)
            if b[0].button(
                "Approve",
                key=f"{key}_approve",
                type="primary",
                disabled=not draft.content.strip(),
                width="stretch",
            ):
                draft.status = DraftStatus.APPROVED
                state.stage = WorkflowStage.APPROVED
                st.rerun()
            if b[1].button("Reject", key=f"{key}_reject", width="stretch"):
                draft.status = DraftStatus.REJECTED
                st.rerun()
            if b[2].button("Skip", key=f"{key}_skip", width="stretch"):
                draft.status = DraftStatus.SKIPPED
                st.rerun()
            if draft.history and b[3].button(
                "Undo edit", key=f"{key}_undo", width="stretch"
            ):
                draft.content = draft.history.pop()
                st.session_state.pop(f"{key}_content", None)
                st.rerun()
            instruction = st.text_input(
                "Instruction for regeneration (optional)",
                key=f"{key}_instr",
                placeholder="e.g. shorter, more formal, include the timeline",
            )
            if st.button(
                "🔄 Regenerate", key=f"{key}_regen", disabled=not c.llm.is_configured()
            ):
                run_with_status(
                    f"Regenerating {draft.target}",
                    lambda p: c.workflow.regenerate(
                        state, c.document_context(), draft.target, instruction, p
                    ),
                )
                st.session_state.pop(f"{key}_content", None)
                st.rerun()
        with right:
            if draft.revision_suggestion:
                st.markdown("**💡 Quality reviewer suggestion**")
                diff = difflib.ndiff(
                    draft.content.split(), draft.revision_suggestion.split()
                )
                changed = " ".join(
                    (
                        f"~~{w[2:]}~~"
                        if w.startswith("- ")
                        else (f"**{w[2:]}**" if w.startswith("+ ") else w[2:])
                    )
                    for w in diff
                    if not w.startswith("? ")
                )
                st.markdown(changed[:3000])
                if st.button("Apply suggestion", key=f"{key}_apply_rev"):
                    WorkflowService.apply_revision(state, draft.target)
                    st.session_state.pop(f"{key}_content", None)
                    st.rerun()
            for w in draft.warnings:
                st.warning(w)
            for claim in draft.unsupported_claims:
                st.error(f"Unsupported (not included): {claim}")
            for need in draft.needs_user_input:
                st.info(f"Needs your input: {need}")
            if draft.sources:
                st.markdown("**Sources**")
                for ref in draft.sources[:12]:
                    st.markdown(
                        f"- {ref.label()}"
                        + (f"  \n  _“{ref.excerpt}”_" if ref.excerpt else "")
                    )
            elif draft.content.strip():
                st.caption("No source references were attached to this content.")
