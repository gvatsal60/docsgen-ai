"""PDF extraction with PyMuPDF (text per page) and pdfplumber (tables)."""

from __future__ import annotations

import logging
from pathlib import Path

from strands import tool

from models.document_models import (
    ExtractedTable,
    SourceChunk,
    SourceDocument,
    SourceRef,
)
from services.security import wrap_untrusted
from tools.context import DocumentContext
from tools.text_extraction import CHUNK_CHARS, extract_entities

logger = logging.getLogger(__name__)
MAX_PAGES = 500
MAX_TABLE_PAGES = 40


def extract_pdf(path: Path, doc_id: str, file_name: str, sha256: str) -> SourceDocument:
    import pymupdf

    warnings: list[str] = []
    chunks: list[SourceChunk] = []
    texts: list[str] = []
    with pymupdf.open(str(path)) as pdf:
        if pdf.needs_pass:
            raise ValueError("The PDF is password protected.")
        page_count = pdf.page_count
        if page_count > MAX_PAGES:
            warnings.append(
                f"Only the first {MAX_PAGES} of {page_count} pages were extracted."
            )
        metadata = {k: v for k, v in (pdf.metadata or {}).items() if v}
        for index in range(min(page_count, MAX_PAGES)):
            page_text = pdf[index].get_text("text").strip()
            texts.append(page_text)
            if not page_text:
                continue
            # split long pages into several chunks, all referencing the page
            parts = [
                page_text[i : i + CHUNK_CHARS]
                for i in range(0, len(page_text), CHUNK_CHARS)
            ]
            for n, part in enumerate(parts, start=1):
                cid = f"{doc_id}-C{len(chunks) + 1}"
                location = f"page {index + 1}" + (
                    f" (part {n})" if len(parts) > 1 else ""
                )
                chunks.append(
                    SourceChunk(
                        chunk_id=cid,
                        doc_id=doc_id,
                        text=part,
                        ref=SourceRef(
                            doc_id=doc_id,
                            file_name=file_name,
                            location=location,
                            chunk_id=cid,
                        ),
                    )
                )
    if not any(texts):
        warnings.append("No text layer was found. Scanned PDFs need OCR before upload.")

    tables: list[ExtractedTable] = []
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as plumber:
            for index, page in enumerate(plumber.pages[:MAX_TABLE_PAGES]):
                for t_index, table in enumerate(page.extract_tables() or [], start=1):
                    rows = [[(c or "").strip() for c in row] for row in table if row]
                    if len(rows) < 2:
                        continue
                    ref = SourceRef(
                        doc_id=doc_id,
                        file_name=file_name,
                        location=f"page {index + 1}, table {t_index}",
                    )
                    tables.append(
                        ExtractedTable(
                            name=f"Page {index + 1} table {t_index}",
                            ref=ref,
                            columns=rows[0],
                            rows=rows[1:200],
                            total_rows=len(rows) - 1,
                        )
                    )
    except Exception as exc:  # noqa: BLE001 - tables are optional
        logger.info("PDF table extraction skipped: %s", type(exc).__name__)
        warnings.append("Table extraction was skipped for this PDF.")

    full_text = "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in enumerate(texts) if t)
    return SourceDocument(
        doc_id=doc_id,
        file_name=file_name,
        file_type="pdf",
        size_bytes=path.stat().st_size,
        sha256=sha256,
        stored_path=str(path),
        text=full_text,
        chunks=chunks,
        tables=tables,
        entities=extract_entities(full_text),
        metadata=metadata,
        pages=page_count,
        warnings=warnings,
    )


def make_get_pdf_page(ctx: DocumentContext):
    @tool(name="get_pdf_page")
    def get_pdf_page(doc_id: str, page: int) -> str:
        """Return the extracted text of one page of an uploaded PDF.

        Args:
            doc_id: Document id of a PDF from list_source_documents.
            page: 1-based page number.
        """
        doc = ctx.source(doc_id)
        if doc.file_type != "pdf":
            return f"{doc_id} is not a PDF."
        prefix = f"page {int(page)}"
        parts = [
            c
            for c in doc.chunks
            if c.ref.location == prefix or c.ref.location.startswith(prefix + " (")
        ]
        if not parts:
            return (
                f"Page {page} has no extracted text (the document has {doc.pages}"
                " pages)."
            )
        return ctx.limit(
            wrap_untrusted(
                f"{doc.file_name} {prefix}", "\n".join(c.text for c in parts)
            )
        )

    return get_pdf_page
