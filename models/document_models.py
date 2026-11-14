"""Pydantic models for templates, source documents and generated files."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    """Traceable reference to a location in an uploaded source document."""

    doc_id: str
    file_name: str
    location: (
        str  # e.g. "page 3", "sheet Budget rows 1-20", "paragraph 12", "lines 40-60"
    )
    chunk_id: str | None = None
    excerpt: str | None = None

    def label(self) -> str:
        return f"{self.file_name} — {self.location}"


# --------------------------------------------------------------------------------- Template
class Placeholder(BaseModel):
    key: str
    raw: str
    count: int = 1
    locations: list[str] = Field(
        default_factory=list
    )  # body / table / header / footer / textbox
    split_across_runs: bool = False


class TemplateSection(BaseModel):
    section_id: str
    title: str
    level: int
    heading_block: (
        int | None
    )  # top-level body block index of the heading (None = preamble)
    body_blocks: list[int] = Field(default_factory=list)
    content_text: str = ""
    is_empty: bool = True
    instructional_blocks: list[int] = Field(default_factory=list)
    placeholders: list[str] = Field(default_factory=list)
    table_indices: list[int] = Field(default_factory=list)

    @property
    def has_real_content(self) -> bool:
        return (
            bool(set(self.body_blocks) - set(self.instructional_blocks))
            and not self.is_empty
        )


class TableInfo(BaseModel):
    index: int
    block: int
    rows: int
    cols: int
    preview: list[list[str]] = Field(default_factory=list)
    section_id: str | None = None
    placeholders: list[str] = Field(default_factory=list)


class HeaderFooterInfo(BaseModel):
    section_index: int
    kind: Literal["header", "footer"]
    variant: Literal["default", "first_page", "even_page"]
    text: str
    placeholders: list[str] = Field(default_factory=list)
    has_images: bool = False


class PageSetup(BaseModel):
    section_index: int
    orientation: str
    page_width_in: float | None = None
    page_height_in: float | None = None
    margins_in: dict[str, float | None] = Field(default_factory=dict)
    start_type: str | None = None


class TemplateStructure(BaseModel):
    file_name: str
    sha256: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    block_count: int = 0
    paragraph_count: int = 0
    sections: list[TemplateSection] = Field(default_factory=list)
    tables: list[TableInfo] = Field(default_factory=list)
    placeholders: list[Placeholder] = Field(default_factory=list)
    headers_footers: list[HeaderFooterInfo] = Field(default_factory=list)
    page_setup: list[PageSetup] = Field(default_factory=list)
    styles_used: list[str] = Field(default_factory=list)
    image_count: int = 0
    hyperlink_count: int = 0
    has_numbered_lists: bool = False
    warnings: list[str] = Field(default_factory=list)

    def section(self, section_id: str) -> TemplateSection | None:
        return next((s for s in self.sections if s.section_id == section_id), None)

    def placeholder_keys(self) -> list[str]:
        return [p.key for p in self.placeholders]

    def outline_text(self, max_chars: int = 20_000) -> str:
        lines = []
        for s in self.sections:
            indent = "  " * max(0, s.level - 1)
            flag = (
                "EMPTY"
                if s.is_empty
                else ("INSTRUCTIONS ONLY" if not s.has_real_content else "HAS CONTENT")
            )
            lines.append(f"{indent}[{s.section_id}] {s.title} ({flag})")
            if s.content_text:
                lines.append(f"{indent}    current text: {s.content_text[:400]}")
            if s.placeholders:
                lines.append(f"{indent}    placeholders: {', '.join(s.placeholders)}")
        if self.placeholders:
            lines.append(
                "Placeholders: "
                + ", ".join(
                    f"{{{{{p.key}}}}} ({'/'.join(p.locations)})"
                    for p in self.placeholders
                )
            )
        text = "\n".join(lines)
        return text[:max_chars]


# ------------------------------------------------------------------------------------ Sources
class ExtractedTable(BaseModel):
    name: str
    ref: SourceRef
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    total_rows: int = 0


class ExtractedEntities(BaseModel):
    dates: list[str] = Field(default_factory=list)
    money: list[str] = Field(default_factory=list)
    percentages: list[str] = Field(default_factory=list)
    numbers: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    candidate_names: list[str] = Field(default_factory=list)


class SourceChunk(BaseModel):
    chunk_id: str
    doc_id: str
    text: str
    ref: SourceRef


class SourceDocument(BaseModel):
    doc_id: str
    file_name: str
    file_type: str
    size_bytes: int
    sha256: str
    stored_path: str
    text: str = ""
    chunks: list[SourceChunk] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)
    entities: ExtractedEntities = Field(default_factory=ExtractedEntities)
    metadata: dict[str, Any] = Field(default_factory=dict)
    pages: int | None = None
    sheets: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    uploaded_at: datetime = Field(default_factory=datetime.now)


# ------------------------------------------------------------------------------- Generation
class FormatCheck(BaseModel):
    check: str
    status: Literal["preserved", "changed_expected", "changed", "info"]
    detail: str = ""


class WriteReport(BaseModel):
    placeholders_replaced: dict[str, int] = Field(default_factory=dict)
    placeholders_remaining: list[str] = Field(default_factory=list)
    sections_written: list[str] = Field(default_factory=list)
    sections_skipped: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    format_checks: list[FormatCheck] = Field(default_factory=list)


class BlockDiff(BaseModel):
    change: Literal["added", "removed", "modified"]
    original: str = ""
    generated: str = ""
    similarity: float = 0.0


class ComparisonReport(BaseModel):
    original_blocks: int
    generated_blocks: int
    added: int
    removed: int
    modified: int
    unchanged: int
    diffs: list[BlockDiff] = Field(default_factory=list)


class GenerationRecord(BaseModel):
    version: int
    run_id: str
    created_at: datetime = Field(default_factory=datetime.now)
    template_name: str
    file_name: str
    docx_path: str
    json_path: str | None = None
    report_path: str | None = None
    sha256: str
    sections_applied: list[str] = Field(default_factory=list)
    placeholders_applied: list[str] = Field(default_factory=list)
    provider_label: str = ""
    validation_summary: str = ""
    write_report: WriteReport = Field(default_factory=WriteReport)
    comparison: ComparisonReport | None = None
