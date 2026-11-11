"""Rendering of the analyzed template structure and extracted source documents."""

from __future__ import annotations

import streamlit as st

from models.document_models import SourceDocument, TemplateStructure
from ui.components import badge


def render_template_structure(structure: TemplateStructure) -> None:
    m = st.columns(6)
    m[0].metric(
        "Sections", sum(1 for s in structure.sections if s.heading_block is not None)
    )
    m[1].metric("Placeholders", len(structure.placeholders))
    m[2].metric("Tables", len(structure.tables))
    m[3].metric("Paragraphs", structure.paragraph_count)
    m[4].metric("Images", structure.image_count)
    m[5].metric("Page sections", len(structure.page_setup))
    for warning in structure.warnings:
        st.info(warning)

    tab_sections, tab_ph, tab_tables, tab_hf, tab_layout = st.tabs(
        ["Sections", "Placeholders", "Tables", "Headers & footers", "Layout & metadata"]
    )
    with tab_sections:
        for s in structure.sections:
            if s.heading_block is None:
                label = s.title
            else:
                label = f"{'  ' * max(0, s.level - 1)}{s.section_id} · {s.title}"
            state = (
                "empty"
                if s.is_empty and not s.instructional_blocks
                else ("instructions only" if not s.has_real_content else "has content")
            )
            kind = {
                "empty": "warning",
                "instructions only": "pending",
                "has content": "ok",
            }[state]
            with st.expander(f"{label}  —  {state}"):
                st.markdown(
                    badge(state, kind)
                    + (
                        "  ·  placeholders:"
                        f" {', '.join('{{' + p + '}}' for p in s.placeholders)}"
                        if s.placeholders
                        else ""
                    )
                )
                st.text(s.content_text[:2000] or "(no text)")
    with tab_ph:
        if structure.placeholders:
            st.dataframe(
                [
                    {
                        "Placeholder": "{{" + p.key + "}}",
                        "Occurrences": p.count,
                        "Locations": ", ".join(p.locations),
                        "Split across runs": "yes" if p.split_across_runs else "",
                    }
                    for p in structure.placeholders
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption(
                "No {{placeholders}} found. Content will be inserted into sections"
                " under their headings."
            )
    with tab_tables:
        if not structure.tables:
            st.caption("No tables.")
        for t in structure.tables:
            st.markdown(
                f"**Table {t.index + 1}** · {t.rows}×{t.cols}"
                + (f" · section {t.section_id}" if t.section_id else "")
            )
            if t.preview:
                width = max(len(r) for r in t.preview)
                st.dataframe(
                    [dict(enumerate(r + [""] * (width - len(r)))) for r in t.preview],
                    hide_index=True,
                    width="stretch",
                )
    with tab_hf:
        if not structure.headers_footers:
            st.caption("No headers or footers.")
        for hf in structure.headers_footers:
            st.markdown(
                f"**Section {hf.section_index + 1} {hf.kind}**"
                f" ({hf.variant.replace('_', ' ')})"
                + (" · contains image/logo" if hf.has_images else "")
            )
            st.text(hf.text or "(empty)")
    with tab_layout:
        st.dataframe(
            [
                {
                    "Section": p.section_index + 1,
                    "Orientation": p.orientation,
                    "Page (in)": f"{p.page_width_in} × {p.page_height_in}",
                    "Margins T/B/L/R (in)": " / ".join(
                        str(p.margins_in.get(k))
                        for k in ("top", "bottom", "left", "right")
                    ),
                    "Start": p.start_type,
                }
                for p in structure.page_setup
            ],
            hide_index=True,
            width="stretch",
        )
        st.markdown("**Styles in use:** " + ", ".join(structure.styles_used))
        st.markdown(
            f"**Hyperlinks:** {structure.hyperlink_count} · **Lists:**"
            f" {'yes' if structure.has_numbered_lists else 'style-based or none'}"
        )
        if structure.metadata:
            st.json(structure.metadata)


def render_source(doc: SourceDocument) -> None:
    m = st.columns(4)
    m[0].metric("Characters", f"{len(doc.text):,}")
    m[1].metric("Chunks", len(doc.chunks))
    m[2].metric("Tables", len(doc.tables))
    m[3].metric("Pages / sheets", doc.pages or len(doc.sheets) or "—")
    for w in doc.warnings:
        st.warning(w)
    t1, t2, t3, t4 = st.tabs(["Text chunks", "Tables", "Entities", "Metadata"])
    with t1:
        for chunk in doc.chunks[:30]:
            st.markdown(f"`{chunk.chunk_id}` · {chunk.ref.location}")
            st.text(chunk.text[:800])
        if len(doc.chunks) > 30:
            st.caption(f"… {len(doc.chunks) - 30} more chunks")
    with t2:
        if not doc.tables:
            st.caption("No tables extracted.")
        for table in doc.tables[:10]:
            st.markdown(
                f"**{table.name}** · {table.ref.location} · {table.total_rows} row(s)"
            )
            cols = [c or f"col{i + 1}" for i, c in enumerate(table.columns)]
            if len(set(cols)) != len(cols):
                cols = [f"{c} ({i + 1})" for i, c in enumerate(cols)]
            st.dataframe(
                [dict(zip(cols, r)) for r in table.rows[:50]],
                hide_index=True,
                width="stretch",
            )
    with t3:
        ent = doc.entities.model_dump()
        st.dataframe(
            [
                {"Type": k.replace("_", " "), "Values": ", ".join(v[:25])}
                for k, v in ent.items()
                if v
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "Pattern-based extraction. The Information Extraction agent performs the"
            " semantic analysis."
        )
    with t4:
        st.json(
            {
                "doc_id": doc.doc_id,
                "file_name": doc.file_name,
                "type": doc.file_type,
                "size_bytes": doc.size_bytes,
                "sha256": doc.sha256[:16] + "…",
                **doc.metadata,
            }
        )
