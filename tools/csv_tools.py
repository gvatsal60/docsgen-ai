"""CSV extraction (pandas, delimiter sniffing) and tools."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pandas as pd
from strands import tool

from models.document_models import ExtractedTable, SourceDocument, SourceRef
from services.security import wrap_untrusted
from tools.context import DocumentContext
from tools.excel_tools import frame_to_rows, numeric_summary, sheet_chunks
from tools.text_extraction import decode_text, extract_entities

MAX_ROWS = 5_000


def extract_csv(path: Path, doc_id: str, file_name: str, sha256: str) -> SourceDocument:
    raw = decode_text(path.read_bytes())
    warnings = []
    try:
        dialect = csv.Sniffer().sniff(raw[:20_000], delimiters=",;\t|")
        sep = dialect.delimiter
    except csv.Error:
        sep = ","
    df = pd.read_csv(
        io.StringIO(raw),
        sep=sep,
        nrows=MAX_ROWS,
        dtype=str,
        keep_default_na=False,
        on_bad_lines="warn",
    )
    if len(df) >= MAX_ROWS:
        warnings.append(f"Only the first {MAX_ROWS} rows were read.")
    columns, rows = frame_to_rows(df)
    name = Path(file_name).stem
    chunks = sheet_chunks(doc_id, file_name, name, columns, rows, 1)
    for c in chunks:
        c.ref.location = c.ref.location.replace(f"sheet '{name}' ", "")
    text = "\n".join(c.text for c in chunks)
    table = ExtractedTable(
        name=name,
        ref=SourceRef(doc_id=doc_id, file_name=file_name, location="table"),
        columns=columns,
        rows=rows[:200],
        total_rows=len(rows),
    )
    return SourceDocument(
        doc_id=doc_id,
        file_name=file_name,
        file_type="csv",
        size_bytes=path.stat().st_size,
        sha256=sha256,
        stored_path=str(path),
        text=text,
        chunks=chunks,
        tables=[table],
        entities=extract_entities(text),
        metadata={"rows": len(rows), "columns": len(columns), "delimiter": sep},
        warnings=warnings,
    )


def make_get_csv_rows(ctx: DocumentContext):
    @tool(name="get_csv_rows")
    def get_csv_rows(
        doc_id: str, start_row: int = 0, max_rows: int = 50, filter_text: str = ""
    ) -> str:
        """Return rows of an uploaded CSV file, optionally only rows containing a text.

        Args:
            doc_id: Document id of a CSV document.
            start_row: 0-based index of the first data row.
            max_rows: Number of rows to return (1-200).
            filter_text: Optional case-insensitive text that a row must contain.
        """
        doc = ctx.source(doc_id)
        if doc.file_type != "csv" or not doc.tables:
            return f"{doc_id} is not a CSV document."
        table = doc.tables[0]
        rows = table.rows
        if filter_text:
            rows = [r for r in rows if any(filter_text.lower() in c.lower() for c in r)]
        start, count = max(0, int(start_row)), max(1, min(int(max_rows), 200))
        data = {
            "columns": table.columns,
            "rows": rows[start : start + count],
            "matching_rows": len(rows),
        }
        return ctx.limit(
            wrap_untrusted(f"{doc.file_name} rows", json.dumps(data, indent=1))
        )

    return get_csv_rows


def make_csv_statistics(ctx: DocumentContext):
    @tool(name="csv_statistics")
    def csv_statistics(doc_id: str) -> str:
        """Compute count, sum, min, max and mean for numeric columns of an uploaded CSV file.

        Args:
            doc_id: Document id of a CSV document.
        """
        doc = ctx.source(doc_id)
        if not doc.tables:
            return "No table found."
        return json.dumps(numeric_summary(doc.tables[0]), indent=1)

    return csv_statistics
