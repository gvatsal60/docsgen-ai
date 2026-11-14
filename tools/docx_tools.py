"""DOCX source extraction and template inspection tools."""

from __future__ import annotations

import json
from pathlib import Path

import docx
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from strands import tool

from document_engine.formatting_preserver import (
    NS,
    element_text,
    iter_body_blocks,
    paragraph_text,
)
from models.document_models import (
    ExtractedTable,
    SourceChunk,
    SourceDocument,
    SourceRef,
)
from services.security import wrap_untrusted
from tools.context import DocumentContext
from tools.text_extraction import CHUNK_CHARS, extract_entities

MAX_TABLE_ROWS = 200


def extract_docx_source(
    path: Path, doc_id: str, file_name: str, sha256: str
) -> SourceDocument:
    """Extract paragraphs (with heading context) and tables from a DOCX source document."""
    document = docx.Document(str(path))
    lines: list[str] = []
    chunks: list[SourceChunk] = []
    tables: list[ExtractedTable] = []
    buf: list[str] = []
    buf_start = 1
    heading = ""
    para_no = 0
    table_no = 0

    def flush(end: int) -> None:
        nonlocal buf, buf_start
        if buf:
            cid = f"{doc_id}-C{len(chunks) + 1}"
            location = f"paragraphs {buf_start}-{end}" + (
                f" (section '{heading[:60]}')" if heading else ""
            )
            chunks.append(
                SourceChunk(
                    chunk_id=cid,
                    doc_id=doc_id,
                    text="\n".join(buf),
                    ref=SourceRef(
                        doc_id=doc_id,
                        file_name=file_name,
                        location=location,
                        chunk_id=cid,
                    ),
                )
            )
        buf, buf_start = [], end + 1

    for el in iter_body_blocks(document.element.body):
        if el.tag == qn("w:p"):
            para_no += 1
            text = paragraph_text(el).strip()
            if not text:
                continue
            style = Paragraph(el, document._body).style
            if style is not None and (style.name or "").startswith(
                ("Heading", "Title")
            ):
                flush(para_no - 1)
                buf_start = para_no
                heading = text
                text = f"## {text}"
            if sum(len(b) for b in buf) + len(text) > CHUNK_CHARS and buf:
                flush(para_no - 1)
            buf.append(text)
            lines.append(text)
        elif el.tag == qn("w:tbl"):
            table_no += 1
            rows = []
            for tr in el.findall("w:tr", NS):
                rows.append([element_text(tc).strip() for tc in tr.findall("w:tc", NS)])
            if not rows:
                continue
            ref = SourceRef(
                doc_id=doc_id, file_name=file_name, location=f"table {table_no}"
            )
            tables.append(
                ExtractedTable(
                    name=f"Table {table_no}",
                    ref=ref,
                    columns=rows[0],
                    rows=rows[1:MAX_TABLE_ROWS],
                    total_rows=len(rows) - 1,
                )
            )
            table_text = "\n".join(" | ".join(r) for r in rows[:MAX_TABLE_ROWS])
            flush(para_no)
            cid = f"{doc_id}-C{len(chunks) + 1}"
            chunks.append(
                SourceChunk(
                    chunk_id=cid,
                    doc_id=doc_id,
                    text=table_text,
                    ref=SourceRef(
                        doc_id=doc_id,
                        file_name=file_name,
                        location=f"table {table_no}",
                        chunk_id=cid,
                    ),
                )
            )
            lines.append(table_text)
    flush(para_no)

    # Headers/footers often hold company names, references and dates.
    hf_text = []
    for section in document.sections:
        for hf in (section.header, section.footer):
            if not hf.is_linked_to_previous:
                t = "\n".join(p.text for p in hf.paragraphs if p.text.strip())
                if t:
                    hf_text.append(t)
    if hf_text:
        cid = f"{doc_id}-C{len(chunks) + 1}"
        chunks.append(
            SourceChunk(
                chunk_id=cid,
                doc_id=doc_id,
                text="\n".join(hf_text),
                ref=SourceRef(
                    doc_id=doc_id,
                    file_name=file_name,
                    location="headers/footers",
                    chunk_id=cid,
                ),
            )
        )

    text = "\n".join(lines + hf_text)
    cp = document.core_properties
    metadata = {
        k: str(v)
        for k, v in {
            "title": cp.title,
            "author": cp.author,
            "created": cp.created,
            "modified": cp.modified,
        }.items()
        if v
    }
    return SourceDocument(
        doc_id=doc_id,
        file_name=file_name,
        file_type="docx",
        size_bytes=path.stat().st_size,
        sha256=sha256,
        stored_path=str(path),
        text=text,
        chunks=chunks,
        tables=tables,
        entities=extract_entities(text),
        metadata=metadata | {"paragraphs": para_no, "tables": table_no},
    )


# ------------------------------------------------------------------------------ tools
def make_get_template_outline(ctx: DocumentContext):
    @tool(name="get_template_outline")
    def get_template_outline() -> str:
        """Return the uploaded Word template's section outline (ids, titles, current text) and placeholders."""
        if ctx.template is None:
            return "No template has been uploaded."
        return ctx.limit(
            wrap_untrusted("template outline", ctx.template.outline_text())
        )

    return get_template_outline


def make_get_template_section(ctx: DocumentContext):
    @tool(name="get_template_section")
    def get_template_section(section_id: str) -> str:
        """Return details of one template section: title, level, current text, placeholders and tables.

        Args:
            section_id: Section id such as s03.
        """
        if ctx.template is None:
            return "No template has been uploaded."
        section = ctx.template.section(section_id.strip().lower())
        if section is None:
            return (
                f"Unknown section '{section_id}'. Valid ids:"
                f" {', '.join(s.section_id for s in ctx.template.sections)}"
            )
        tables = [
            t.model_dump()
            for t in ctx.template.tables
            if t.section_id == section.section_id
        ]
        data = section.model_dump(
            include={
                "section_id",
                "title",
                "level",
                "content_text",
                "is_empty",
                "placeholders",
            }
        ) | {"tables": tables}
        return ctx.limit(
            wrap_untrusted(
                f"template section {section.section_id}", json.dumps(data, indent=1)
            )
        )

    return get_template_section


def make_list_template_placeholders(ctx: DocumentContext):
    @tool(name="list_template_placeholders")
    def list_template_placeholders() -> str:
        """List the {{placeholders}} found in the template with their locations.

        Locations are body, table, header, footer or textbox.
        """
        if ctx.template is None:
            return "No template has been uploaded."
        return json.dumps(
            [
                p.model_dump(include={"key", "count", "locations"})
                for p in ctx.template.placeholders
            ],
            indent=1,
        )

    return list_template_placeholders


def make_get_document_tables(ctx: DocumentContext):
    @tool(name="get_document_tables")
    def get_document_tables(doc_id: str, max_rows: int = 50) -> str:
        """Return the tables extracted from a Word or PDF source document.

        Args:
            doc_id: Document id from list_source_documents.
            max_rows: Maximum rows per table (1-200).
        """
        doc = ctx.source(doc_id)
        max_rows = max(1, min(int(max_rows), 200))
        if not doc.tables:
            return "This document has no extracted tables."
        out = []
        for t in doc.tables:
            out.append(
                {
                    "name": t.name,
                    "location": t.ref.location,
                    "columns": t.columns,
                    "rows": t.rows[:max_rows],
                    "total_rows": t.total_rows,
                }
            )
        return ctx.limit(
            wrap_untrusted(f"tables of {doc.file_name}", json.dumps(out, indent=1))
        )

    return get_document_tables
