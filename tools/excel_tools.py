"""Excel (XLSX via openpyxl, XLS via xlrd) extraction and tools. Formulas are never evaluated or executed;
cached values stored in the file are used."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
from strands import tool

from models.document_models import (
    ExtractedTable,
    SourceChunk,
    SourceDocument,
    SourceRef,
)
from services.security import wrap_untrusted
from tools.context import DocumentContext
from tools.text_extraction import extract_entities

MAX_ROWS = 2_000
ROWS_PER_CHUNK = 40


def _cell(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def frame_to_rows(df: pd.DataFrame) -> tuple[list[str], list[list[str]]]:
    columns = [_cell(c) if not str(c).startswith("Unnamed") else "" for c in df.columns]
    rows = [[_cell(v) for v in row] for row in df.itertuples(index=False, name=None)]
    return columns, rows


def sheet_chunks(
    doc_id: str,
    file_name: str,
    sheet: str,
    columns: list[str],
    rows: list[list[str]],
    start_index: int,
) -> list[SourceChunk]:
    chunks: list[SourceChunk] = []
    header = " | ".join(columns)
    for i in range(0, max(len(rows), 1), ROWS_PER_CHUNK):
        block = rows[i : i + ROWS_PER_CHUNK]
        text = "\n".join([f"Sheet: {sheet}", header] + [" | ".join(r) for r in block])
        cid = f"{doc_id}-C{start_index + len(chunks)}"
        # +2: header is spreadsheet row 1, first data row is row 2
        location = f"sheet '{sheet}' rows {i + 2}-{i + 1 + max(len(block), 1)}"
        chunks.append(
            SourceChunk(
                chunk_id=cid,
                doc_id=doc_id,
                text=text,
                ref=SourceRef(
                    doc_id=doc_id, file_name=file_name, location=location, chunk_id=cid
                ),
            )
        )
    return chunks


def extract_spreadsheet(
    path: Path, doc_id: str, file_name: str, sha256: str
) -> SourceDocument:
    ext = path.suffix.lower()
    engine = "openpyxl" if ext == ".xlsx" else "xlrd"
    sheets = pd.read_excel(path, sheet_name=None, engine=engine, nrows=MAX_ROWS)
    warnings: list[str] = []
    chunks: list[SourceChunk] = []
    tables: list[ExtractedTable] = []
    texts = []
    for name, df in sheets.items():
        df = df.dropna(how="all").dropna(axis=1, how="all")
        columns, rows = frame_to_rows(df)
        if len(df) >= MAX_ROWS:
            warnings.append(f"Sheet '{name}' was truncated to {MAX_ROWS} rows.")
        if ext == ".xlsx" and df.isna().any().any():
            warnings.append(
                f"Sheet '{name}' has empty cells; formula cells without saved values"
                " appear empty."
            )
        new_chunks = sheet_chunks(
            doc_id, file_name, str(name), columns, rows, len(chunks) + 1
        )
        chunks.extend(new_chunks)
        ref = SourceRef(doc_id=doc_id, file_name=file_name, location=f"sheet '{name}'")
        tables.append(
            ExtractedTable(
                name=str(name),
                ref=ref,
                columns=columns,
                rows=rows[:200],
                total_rows=len(rows),
            )
        )
        texts.append("\n".join(c.text for c in new_chunks))
    text = "\n\n".join(texts)
    return SourceDocument(
        doc_id=doc_id,
        file_name=file_name,
        file_type=ext.lstrip("."),
        size_bytes=path.stat().st_size,
        sha256=sha256,
        stored_path=str(path),
        text=text,
        chunks=chunks,
        tables=tables,
        entities=extract_entities(text),
        sheets=[str(s) for s in sheets],
        warnings=warnings,
        metadata={"sheets": len(sheets)},
    )


def numeric_summary(table: ExtractedTable) -> dict:
    df = pd.DataFrame(
        table.rows,
        columns=(
            table.columns if len(set(table.columns)) == len(table.columns) else None
        ),
    )
    summary = {}
    for col in df.columns:
        values = pd.to_numeric(
            df[col]
            .astype(str)
            .str.replace(",", "")
            .str.replace(r"[^\d.\-]", "", regex=True),
            errors="coerce",
        ).dropna()
        if len(values) and len(values) >= max(1, len(df) // 2):
            summary[str(col)] = {
                "count": int(values.count()),
                "sum": float(values.sum()),
                "min": float(values.min()),
                "max": float(values.max()),
                "mean": round(float(values.mean()), 2),
            }
    return summary


def make_get_spreadsheet_sheet(ctx: DocumentContext):
    @tool(name="get_spreadsheet_sheet")
    def get_spreadsheet_sheet(
        doc_id: str, sheet: str, start_row: int = 0, max_rows: int = 50
    ) -> str:
        """Return rows from a sheet of an uploaded Excel workbook (or the single table of a CSV file).

        Args:
            doc_id: Document id of an XLSX, XLS or CSV document.
            sheet: Sheet name (use the file name for CSV files).
            start_row: 0-based index of the first data row to return.
            max_rows: Number of rows to return (1-200).
        """
        doc = ctx.source(doc_id)
        table = next((t for t in doc.tables if t.name == sheet), None) or (
            doc.tables[0] if len(doc.tables) == 1 else None
        )
        if table is None:
            return (
                f"Unknown sheet '{sheet}'. Sheets:"
                f" {', '.join(t.name for t in doc.tables)}"
            )
        start, count = max(0, int(start_row)), max(1, min(int(max_rows), 200))
        data = {
            "sheet": table.name,
            "columns": table.columns,
            "rows": table.rows[start : start + count],
            "total_rows": table.total_rows,
            "first_row_number": start + 2,
        }
        return ctx.limit(
            wrap_untrusted(
                f"{doc.file_name} {table.ref.location}", json.dumps(data, indent=1)
            )
        )

    return get_spreadsheet_sheet


def make_spreadsheet_statistics(ctx: DocumentContext):
    @tool(name="spreadsheet_statistics")
    def spreadsheet_statistics(doc_id: str, sheet: str = "") -> str:
        """Compute count, sum, min, max and mean for numeric columns of a sheet or CSV table.

        Args:
            doc_id: Document id of an XLSX, XLS or CSV document.
            sheet: Sheet name; leave empty to summarize every sheet.
        """
        doc = ctx.source(doc_id)
        tables = [t for t in doc.tables if not sheet or t.name == sheet]
        if not tables:
            return f"No table named '{sheet}'."
        return ctx.limit(
            json.dumps(
                {
                    t.name: {
                        "location": t.ref.location,
                        "numeric_columns": numeric_summary(t),
                    }
                    for t in tables
                },
                indent=1,
            )
        )

    return spreadsheet_statistics
