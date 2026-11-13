"""Dispatch uploaded source files to the dedicated extraction modules."""

from __future__ import annotations

import logging
from pathlib import Path

from models.document_models import SourceDocument
from services.file_service import StoredFile
from services.security import sanitize_error
from tools.csv_tools import extract_csv
from tools.docx_tools import extract_docx_source
from tools.excel_tools import extract_spreadsheet
from tools.pdf_tools import extract_pdf
from tools.text_extraction import extract_text_file

logger = logging.getLogger(__name__)

EXTRACTORS = {
    "docx": extract_docx_source,
    "pdf": extract_pdf,
    "txt": extract_text_file,
    "xlsx": extract_spreadsheet,
    "xls": extract_spreadsheet,
    "csv": extract_csv,
}


class ExtractionError(RuntimeError):
    pass


class ExtractionService:
    def __init__(self) -> None:
        self._counter = 0

    def next_doc_id(self, existing: set[str]) -> str:
        while True:
            self._counter += 1
            doc_id = f"D{self._counter}"
            if doc_id not in existing:
                return doc_id

    def extract(self, stored: StoredFile, existing_ids: set[str]) -> SourceDocument:
        extractor = EXTRACTORS.get(stored.file_type)
        if extractor is None:
            raise ExtractionError(
                f"No extractor for .{stored.file_type} files.")
        doc_id = self.next_doc_id(existing_ids)
        try:
            doc = extractor(
                Path(stored.path), doc_id, stored.original_name, stored.sha256
            )
        except Exception as exc:
            logger.info(
                "Extraction failed for %s: %s",
                stored.original_name,
                sanitize_error(exc),
            )
            raise ExtractionError(
                f"Could not read {stored.original_name}: {sanitize_error(exc, 200)}"
            ) from exc
        if not doc.text.strip():
            doc.warnings.append(
                "No text could be extracted from this document.")
        return doc
