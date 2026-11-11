"""Document Generation and History & Downloads pages."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from models.agent_models import WRITING_TASKS, WorkflowStage
from models.document_models import GenerationRecord
from services.container import ServiceContainer
from services.workflow_service import WorkflowService
from ui.components import badge, goto_button, page_header, run_with_status, show_flashes

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _read(path: str | None) -> bytes:
    if not path:
        return b""
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def render_generation(c: ServiceContainer) -> None:
    page_header(
        "Document Generation",
        "Populate the original template with the approved content and download the"
        " result.",
        "🏗️",
    )
    show_flashes()
    state = c.session.workflow
    if state is None:
        st.info("Run the agent workflow first.")
        goto_button("Open the agent workflow", "Agent Workflow", key="gen_go_wf")
        return

    if state.task_type not in WRITING_TASKS or (
        state.stage == WorkflowStage.COMPLETED and not state.drafts
    ):
        _non_writing_outputs(state)
        return

    approved = state.approved_drafts
    st.markdown(
        f"**Approved items:** {len(approved)} of {len(state.drafts)}  ·  **Template:**"
        f" {c.session.template.file_name if c.session.template else '—'}"
    )
    if not approved:
        st.warning(
            "Approve sections or placeholders on the Document Review page first."
        )
        goto_button("Open Document Review", "Document Review", key="gen_go_rv")
        return
    with st.expander("Approved content", expanded=False):
        for d in approved:
            st.markdown(f"**{d.title}** (`{d.target}`)")
            st.text(d.content[:600] + ("…" if len(d.content) > 600 else ""))
    ai_validation = st.toggle(
        "Run the Document Validator agent on the approved content before writing",
        value=c.llm.is_configured(),
        disabled=not c.llm.is_configured(),
        help="Automated value, placeholder and duplicate checks always run.",
    )
    if st.button("🏗️ Generate final document", type="primary"):
        version = len(c.session.history) + 1

        def _go(progress):
            return c.workflow.generate_document(
                state,
                c.document_context(),
                version,
                c.llm.describe(),
                ai_validation=ai_validation,
                progress=progress,
            )

        record = run_with_status("Generating document", _go)
        if record is not None:
            c.session.history.append(record)
            st.session_state["last_record_version"] = record.version
            st.rerun()

    version = st.session_state.get("last_record_version")
    record = next((r for r in c.session.history if r.version == version), None)
    if record is not None:
        _record_details(record, state)


def _record_details(record: GenerationRecord, state) -> None:
    st.success(f"Generated **{record.file_name}** · {record.validation_summary}")
    d = st.columns(3)
    d[0].download_button(
        "⬇️ Download DOCX",
        _read(record.docx_path),
        file_name=record.file_name,
        mime=DOCX_MIME,
        type="primary",
        width="stretch",
        key=f"dl_docx_{record.version}",
    )
    d[1].download_button(
        "⬇️ JSON export",
        _read(record.json_path),
        file_name=Path(record.json_path or "export.json").name,
        mime="application/json",
        width="stretch",
        key=f"dl_json_{record.version}",
    )
    d[2].download_button(
        "⬇️ Completion report",
        _read(record.report_path),
        file_name=Path(record.report_path or "report.md").name,
        mime="text/markdown",
        width="stretch",
        key=f"dl_rep_{record.version}",
    )

    wr = record.write_report
    t1, t2, t3, t4 = st.tabs(
        [
            "Formatting preservation",
            "Completion",
            "Validation",
            "Original vs. generated",
        ]
    )
    with t1:
        st.dataframe(
            [
                {
                    "Status": {
                        "preserved": "✅ preserved",
                        "changed_expected": "✏️ changed as intended",
                        "changed": "⚠️ changed",
                        "info": "ℹ️",
                    }[c.status],
                    "Check": c.check,
                    "Detail": c.detail,
                }
                for c in wr.format_checks
            ],
            hide_index=True,
            width="stretch",
        )
    with t2:
        st.markdown(
            "**Sections written:** " + (", ".join(wr.sections_written) or "none")
        )
        st.markdown(
            "**Placeholders replaced:** "
            + (
                ", ".join(
                    f"{{{{{k}}}}}×{v}" for k, v in wr.placeholders_replaced.items()
                )
                or "none"
            )
        )
        if wr.placeholders_remaining:
            st.warning(
                "Placeholders still in the document: "
                + ", ".join(f"{{{{{k}}}}}" for k in wr.placeholders_remaining)
            )
        for w in wr.warnings + (
            [f"Skipped: {', '.join(wr.sections_skipped)}"]
            if wr.sections_skipped
            else []
        ):
            st.warning(w)
    with t3:
        report = state.final_validation if state else None
        if report:
            score_badge = badge(
                f"{report.completeness_score:.0f}% complete",
                "ok" if report.complete else "warning",
            )
            st.markdown(f"{score_badge} {report.summary}")
            if report.issues:
                st.dataframe(
                    [i.model_dump() for i in report.issues],
                    hide_index=True,
                    width="stretch",
                )
    with t4:
        cmp = record.comparison
        if cmp:
            st.caption(
                f"Blocks: {cmp.original_blocks} → {cmp.generated_blocks} · added"
                f" {cmp.added} · removed {cmp.removed} · modified {cmp.modified} ·"
                f" unchanged {cmp.unchanged}"
            )
            for diff in cmp.diffs[:80]:
                cols = st.columns(2)
                strike = "~~" if diff.change == "removed" else ""
                original = diff.original[:500] or "—"
                cols[0].markdown(f"{strike}{original}{strike}")
                cols[1].markdown(
                    f"**{diff.generated[:500]}**" if diff.change != "removed" else "—"
                )
                st.divider()


def _non_writing_outputs(state) -> None:
    st.info(
        "This task does not modify the template. Its results can be downloaded here."
    )
    if state.extraction:
        st.download_button(
            "⬇️ Extracted information (JSON)",
            state.extraction.model_dump_json(indent=2),
            file_name=f"extraction_{state.run_id}.json",
            mime="application/json",
        )
    if state.summary_text:
        st.markdown(state.summary_text)
        st.download_button(
            "⬇️ Summary (Markdown)",
            state.summary_text,
            file_name=f"summary_{state.run_id}.md",
            mime="text/markdown",
        )
    if state.validation:
        st.download_button(
            "⬇️ Validation report (JSON)",
            state.validation.model_dump_json(indent=2),
            file_name=f"validation_{state.run_id}.json",
            mime="application/json",
        )
    if state.comparison_text:
        st.markdown(state.comparison_text)
        st.download_button(
            "⬇️ Comparison (Markdown)",
            state.comparison_text,
            file_name=f"comparison_{state.run_id}.md",
            mime="text/markdown",
        )
    st.download_button(
        "⬇️ Full run export (JSON)",
        WorkflowService.export_json(state),
        file_name=f"run_{state.run_id}.json",
        mime="application/json",
    )


def render_history(c: ServiceContainer) -> None:
    page_header(
        "History and Downloads",
        "Every document generated in this session, with its reports.",
        "🗂️",
    )
    show_flashes()
    history = c.session.history
    if not history:
        st.info("No documents generated in this session yet.")
        return
    st.dataframe(
        [
            {
                "Version": r.version,
                "File": r.file_name,
                "Created": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "Sections": len(r.sections_applied),
                "Placeholders": len(r.placeholders_applied),
                "Validation": r.validation_summary,
                "Model": r.provider_label,
                "SHA-256": r.sha256[:12],
            }
            for r in reversed(history)
        ],
        hide_index=True,
        width="stretch",
    )
    for r in reversed(history):
        with st.expander(f"v{r.version} · {r.file_name} · {r.created_at:%H:%M:%S}"):
            cols = st.columns(3)
            cols[0].download_button(
                "DOCX",
                _read(r.docx_path),
                file_name=r.file_name,
                mime=DOCX_MIME,
                key=f"h_docx_{r.version}",
                width="stretch",
            )
            cols[1].download_button(
                "JSON",
                _read(r.json_path),
                file_name=Path(r.json_path or "x.json").name,
                mime="application/json",
                key=f"h_json_{r.version}",
                width="stretch",
            )
            cols[2].download_button(
                "Report",
                _read(r.report_path),
                file_name=Path(r.report_path or "x.md").name,
                mime="text/markdown",
                key=f"h_rep_{r.version}",
                width="stretch",
            )
            preserved = sum(
                1 for chk in r.write_report.format_checks if chk.status == "preserved"
            )
            st.caption(
                f"Formatting checks: {preserved}/{len(r.write_report.format_checks)}"
                f" preserved unchanged; sections {', '.join(r.sections_applied) or '—'}"
            )
    if len(history) > 1:
        st.markdown("#### Compare two versions")
        a, b = st.columns(2)
        va = a.selectbox(
            "Version A",
            [r.version for r in history],
            index=len(history) - 2,
            key="cmp_a",
        )
        vb = b.selectbox(
            "Version B",
            [r.version for r in history],
            index=len(history) - 1,
            key="cmp_b",
        )
        if st.button("Compare versions"):
            from document_engine.document_comparator import unified_text_diff

            ra = next(r for r in history if r.version == va)
            rb = next(r for r in history if r.version == vb)
            st.code(
                unified_text_diff(ra.docx_path, rb.docx_path)[:30_000]
                or "No differences.",
                language="diff",
            )
    st.caption(
        "History is kept for this browser session. Generated files are stored under"
        f" {c.settings.generated_dir}."
    )
