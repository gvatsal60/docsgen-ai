"""Document Template Upload and Source Documents pages."""

from __future__ import annotations

import streamlit as st

from document_engine.template_parser import parse_template
from services.container import ServiceContainer, UploadedTemplate
from services.extraction_service import ExtractionError
from services.file_service import (
    ALLOWED_SOURCE_TYPES,
    ALLOWED_TEMPLATE_TYPES,
    UploadRejected,
)
from services.security import sanitize_error
from ui.components import flash, goto_button, page_header, show_flashes
from ui.document_preview import render_source, render_template_structure


def _uploader_key(name: str) -> str:
    return f"{name}_{st.session_state.get(f'{name}_gen', 0)}"


def _reset_uploader(name: str) -> None:
    st.session_state[f"{name}_gen"] = st.session_state.get(f"{name}_gen", 0) + 1


def render_template(c: ServiceContainer) -> None:
    page_header(
        "Document Template",
        "Upload the Word template to populate. DocsGen fills this file in place and"
        " preserves its formatting.",
        "📄",
    )
    show_flashes()
    current = c.session.template
    uploaded = st.file_uploader(
        "DOCX template",
        type=sorted(ALLOWED_TEMPLATE_TYPES),
        key=_uploader_key("tpl_upload"),
        help=(
            f"Maximum {c.settings.max_upload_mb} MB. Macro-enabled files are rejected."
        ),
    )
    if uploaded is not None:
        try:
            stored = c.files.store(
                uploaded.name, uploaded.getvalue(), ALLOWED_TEMPLATE_TYPES
            )
            structure = parse_template(stored.path, stored.original_name)
        except UploadRejected as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001
            st.error(f"The template could not be analyzed: {sanitize_error(exc)}")
        else:
            if current is not None:
                c.files.delete(current.path)
            c.session.template = UploadedTemplate(
                file_id=stored.file_id,
                file_name=stored.original_name,
                path=stored.path,
                structure=structure,
            )
            c.session.workflow = None  # a new template invalidates previous drafts
            _reset_uploader("tpl_upload")
            flash(f"Template '{stored.original_name}' uploaded and analyzed.")
            st.rerun()

    current = c.session.template
    if current is None:
        st.info(
            "No template uploaded yet. Try `sample_data/sample_template.docx` from the"
            " project folder."
        )
        return
    top = st.columns([4, 1, 1])
    top[0].markdown(f"**Current template:** {current.file_name}")
    with top[1]:
        goto_button(
            "Next: sources →", "Source Documents", key="tpl_next", width="stretch"
        )
    if top[2].button("Remove", key="tpl_remove", width="stretch"):
        c.files.delete(current.path)
        c.session.template = None
        c.session.workflow = None
        st.rerun()
    render_template_structure(current.structure)


def render_sources(c: ServiceContainer) -> None:
    page_header(
        "Source Documents",
        "Upload the documents the agents should extract information from (DOCX, PDF,"
        " TXT, XLSX, XLS, CSV).",
        "📚",
    )
    show_flashes()
    uploads = st.file_uploader(
        "Source files",
        type=sorted(ALLOWED_SOURCE_TYPES),
        accept_multiple_files=True,
        key=_uploader_key("src_upload"),
        help=(
            f"Up to {c.settings.max_source_files} files, {c.settings.max_upload_mb} MB"
            " each."
        ),
    )
    if uploads:
        added, errors = [], []
        for uploaded in uploads:
            if len(c.session.sources) >= c.settings.max_source_files:
                errors.append(
                    f"{uploaded.name}: the limit of {c.settings.max_source_files} files"
                    " was reached."
                )
                continue
            data = uploaded.getvalue()
            try:
                stored = c.files.store(uploaded.name, data, ALLOWED_SOURCE_TYPES)
                if any(d.sha256 == stored.sha256 for d in c.session.sources.values()):
                    c.files.delete(stored.path)
                    errors.append(f"{uploaded.name}: already uploaded.")
                    continue
                with st.spinner(f"Extracting {stored.original_name}…"):
                    doc = c.extraction.extract(stored, set(c.session.sources))
            except (UploadRejected, ExtractionError) as exc:
                errors.append(f"{uploaded.name}: {exc}")
                continue
            c.session.sources[doc.doc_id] = doc
            added.append(doc.file_name)
        _reset_uploader("src_upload")
        if added:
            flash(f"Extracted {len(added)} document(s): {', '.join(added)}.")
        for e in errors:
            flash(e, "error")
        st.rerun()

    if not c.session.sources:
        st.info(
            "No source documents yet. Sample files are in `sample_data/` (source text,"
            " budget CSV/XLSX, specification PDF, company profile DOCX)."
        )
        return
    total_chars = sum(len(d.text) for d in c.session.sources.values())
    st.caption(
        f"{len(c.session.sources)} document(s), {total_chars:,} characters extracted."
        f" Agents receive up to {c.settings.agent_context_chars:,} characters directly"
        " and can read the rest with tools."
    )
    for doc in list(c.session.sources.values()):
        with st.expander(
            f"{doc.doc_id} · {doc.file_name} ({doc.file_type.upper()},"
            f" {doc.size_bytes / 1024:.0f} KB)" + (" ⚠️" if doc.warnings else "")
        ):
            render_source(doc)
            if st.button("Remove document", key=f"rm_src_{doc.doc_id}"):
                c.files.delete(doc.stored_path)
                c.session.sources.pop(doc.doc_id, None)
                st.rerun()
    goto_button(
        "Next: AI instructions →", "AI Instructions", key="src_next", type="primary"
    )
