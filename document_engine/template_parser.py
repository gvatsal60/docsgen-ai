"""Analyze a DOCX template: headings, sections, paragraphs, tables, placeholders, headers/footers,
metadata and page setup.

Sections are derived from heading paragraphs (``Heading N``/``Title`` styles or an outline level).
If a template has no styled headings, short bold paragraphs are used as a fallback.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import docx
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.shared import Emu
from docx.text.paragraph import Paragraph
from lxml import etree

from document_engine.formatting_preserver import (
    NS,
    element_text,
    has_drawing,
    has_page_break,
    has_section_break,
    iter_body_blocks,
    paragraph_text,
)
from document_engine.placeholder_manager import (
    PLACEHOLDER_RE,
    document_parts,
    find_placeholders,
)
from models.document_models import (
    HeaderFooterInfo,
    PageSetup,
    Placeholder,
    TableInfo,
    TemplateSection,
    TemplateStructure,
)

INSTRUCTION_PATTERNS = [
    re.compile(r"^\s*\[[^\]]{2,}\]\s*$"),
    re.compile(r"^\s*<[^>]{2,}>\s*$"),
    re.compile(
        r"^\s*(insert|enter|describe|provide|add|type|write|list|summari[sz]e|explain|include|state|outline|detail|"
        r"identify|specify|document|define|give|discuss|present|propose|fill"
        r" in|complete|replace)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bthis"
        r" (section|chapter|part)\b.{0,40}\b(should|will|must|shall|provides?|presents?|contains?|describes?|includes?|synthesi[sz]es|proposes)\b",  # noqa: E501
        re.IGNORECASE,
    ),
    re.compile(r"lorem ipsum", re.IGNORECASE),
    re.compile(
        r"^\s*(tbd|todo|to be (completed|added|determined|confirmed))\b", re.IGNORECASE
    ),
    re.compile(r"\bminimum requirement\b", re.IGNORECASE),
    re.compile(r"^\s*(placeholder|sample text|example text)\b", re.IGNORECASE),
]


def is_instructional(text: str) -> bool:
    text = text.strip()
    if not text or len(text) > 700:
        return False
    return any(p.search(text) for p in INSTRUCTION_PATTERNS)


@dataclass
class Block:
    index: int
    element: etree._Element
    kind: str  # "p" | "tbl" | "sdt"
    text: str
    style_name: str = ""
    heading_level: int | None = None
    instructional: bool = False
    empty: bool = False
    structural: bool = False


def _style_heading_level(paragraph: Paragraph) -> int | None:
    style = paragraph.style
    name = (style.name if style is not None else "") or ""
    if name in ("Title", "Subtitle"):
        return None  # cover title: part of the document start, not a section
    m = re.match(r"^(Heading|heading)\s*(\d)$", name)
    if m:
        return int(m.group(2))
    outline = paragraph._p.find("w:pPr/w:outlineLvl", NS)
    if outline is None and style is not None:
        outline = style.element.find("w:pPr/w:outlineLvl", NS)
        # follow basedOn chain once (common for custom heading styles)
        if outline is None and style.base_style is not None:
            outline = style.base_style.element.find("w:pPr/w:outlineLvl", NS)
    if outline is not None:
        try:
            level = int(outline.get(qn("w:val")))
        except (TypeError, ValueError):
            return None
        return level + 1 if level < 9 else None
    return None


def _looks_like_fallback_heading(paragraph: Paragraph, text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 100 or stripped.endswith((".", ",", ";")):
        return False
    if "\n" in stripped:
        return False
    runs = [r for r in paragraph.runs if r.text.strip()]
    if not runs:
        return False
    bold = all(
        r.bold
        or (
            r.bold is None and paragraph.style is not None and paragraph.style.font.bold
        )
        for r in runs
    )
    return bool(bold and (stripped.isupper() or len(stripped.split()) <= 10))


def build_blocks(doc: DocxDocument) -> list[Block]:
    blocks: list[Block] = []
    body = doc.element.body
    for i, el in enumerate(iter_body_blocks(body)):
        if el.tag == qn("w:p"):
            para = Paragraph(el, doc._body)
            text = paragraph_text(el)
            style_name = (para.style.name if para.style is not None else "") or ""
            level = _style_heading_level(para) if text.strip() else None
            structural = has_section_break(el) or has_page_break(el) or has_drawing(el)
            blocks.append(
                Block(
                    index=i,
                    element=el,
                    kind="p",
                    text=text,
                    style_name=style_name,
                    heading_level=level,
                    instructional=is_instructional(text) and level is None,
                    empty=not text.strip() and not has_drawing(el),
                    structural=structural,
                )
            )
        else:
            kind = "tbl" if el.tag == qn("w:tbl") else "sdt"
            blocks.append(Block(index=i, element=el, kind=kind, text=element_text(el)))
    if not any(b.heading_level is not None for b in blocks):
        for b in blocks:
            if b.kind == "p" and _looks_like_fallback_heading(
                Paragraph(b.element, doc._body), b.text
            ):
                b.heading_level = 1
                b.instructional = False
    return blocks


def build_sections(blocks: list[Block]) -> list[TemplateSection]:
    sections: list[TemplateSection] = []
    current = TemplateSection(
        section_id="s00", title="(Document start)", level=0, heading_block=None
    )
    counter = 0

    def close(section: TemplateSection) -> None:
        body = [b for b in blocks if b.index in section.body_blocks]
        texts = [b.text.strip() for b in body if b.text.strip()]
        section.content_text = "\n".join(texts)[:4000]
        real = [b for b in body if b.text.strip() and not b.instructional]
        section.is_empty = not real
        section.instructional_blocks = [b.index for b in body if b.instructional]
        keys: list[str] = []
        for b in body:
            keys += PLACEHOLDER_RE.findall(b.text)
        section.placeholders = sorted(set(keys))
        if section.heading_block is not None or section.body_blocks:
            sections.append(section)

    for b in blocks:
        if b.heading_level is not None:
            close(current)
            counter += 1
            current = TemplateSection(
                section_id=f"s{counter:02d}",
                title=b.text.strip().replace("\n", " ")[:200],
                level=b.heading_level,
                heading_block=b.index,
            )
        else:
            current.body_blocks.append(b.index)
    close(current)
    return sections


def _inches(value: Emu | int | None) -> float | None:
    return round(Emu(value).inches, 2) if value is not None else None


def analyze_document(
    doc: DocxDocument, file_name: str, sha256: str
) -> tuple[TemplateStructure, list[Block]]:
    blocks = build_blocks(doc)
    sections = build_sections(blocks)
    section_of_block = {idx: s.section_id for s in sections for idx in s.body_blocks}

    tables: list[TableInfo] = []
    table_counter = 0
    for b in blocks:
        if b.kind != "tbl":
            continue
        rows = b.element.findall("w:tr", NS)
        preview = []
        max_cols = 0
        for tr in rows[:8]:
            cells = [element_text(tc).strip()[:60] for tc in tr.findall("w:tc", NS)]
            max_cols = max(max_cols, len(cells))
            preview.append(cells)
        tables.append(
            TableInfo(
                index=table_counter,
                block=b.index,
                rows=len(rows),
                cols=max(max_cols, len(b.element.findall("w:tblGrid/w:gridCol", NS))),
                preview=preview,
                section_id=section_of_block.get(b.index),
                placeholders=sorted(set(PLACEHOLDER_RE.findall(b.text))),
            )
        )
        for sec in sections:
            if sec.section_id == section_of_block.get(b.index):
                sec.table_indices.append(table_counter)
        table_counter += 1

    placeholders: dict[str, Placeholder] = {}
    for hit in find_placeholders(doc):
        ph = placeholders.setdefault(
            hit.key, Placeholder(key=hit.key, raw=hit.raw, count=0)
        )
        ph.count += 1
        if hit.location not in ph.locations:
            ph.locations.append(hit.location)
        ph.split_across_runs = ph.split_across_runs or hit.split_across_runs

    headers_footers: list[HeaderFooterInfo] = []
    for idx, section in enumerate(doc.sections):
        for kind in ("header", "footer"):
            for variant, attr in (
                ("default", kind),
                ("first_page", f"first_page_{kind}"),
                ("even_page", f"even_page_{kind}"),
            ):
                hf = getattr(section, attr)
                if (
                    hf.is_linked_to_previous
                ):  # do not touch: accessing paragraphs would create a new part
                    continue
                root = hf._element
                text = "\n".join(
                    paragraph_text(p) for p in root.iter(qn("w:p"))
                ).strip()
                headers_footers.append(
                    HeaderFooterInfo(
                        section_index=idx,
                        kind=kind,  # type: ignore[arg-type]
                        variant=variant,  # type: ignore[arg-type]
                        text=text[:500],
                        placeholders=sorted(set(PLACEHOLDER_RE.findall(text))),
                        has_images=has_drawing(root),
                    )
                )

    page_setup = []
    for idx, section in enumerate(doc.sections):
        page_setup.append(
            PageSetup(
                section_index=idx,
                orientation="landscape" if section.orientation == 1 else "portrait",
                page_width_in=_inches(section.page_width),
                page_height_in=_inches(section.page_height),
                margins_in={
                    "top": _inches(section.top_margin),
                    "bottom": _inches(section.bottom_margin),
                    "left": _inches(section.left_margin),
                    "right": _inches(section.right_margin),
                },
                start_type=(
                    str(section.start_type).split(".")[-1].split(" ")[0]
                    if section.start_type is not None
                    else None
                ),
            )
        )

    cp = doc.core_properties
    metadata = {
        k: str(v) if v is not None else None
        for k, v in {
            "title": cp.title,
            "subject": cp.subject,
            "author": cp.author,
            "keywords": cp.keywords,
            "category": cp.category,
            "comments": cp.comments,
            "created": cp.created,
            "modified": cp.modified,
            "last_modified_by": cp.last_modified_by,
            "revision": cp.revision,
        }.items()
        if v not in (None, "")
    }

    body = doc.element.body
    image_count = 0
    hyperlink_count = 0
    for _, _, root in document_parts(doc):
        image_count += sum(
            1
            for _ in root.iter(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
            )
        )
        hyperlink_count += sum(1 for _ in root.iter(qn("w:hyperlink")))
    warnings = []
    if not any(s.heading_block is not None for s in sections):
        warnings.append(
            "No headings were found; section-based insertion is limited. Placeholders"
            " still work."
        )
    if any(p.split_across_runs for p in placeholders.values()):
        warnings.append(
            "Some placeholders are split across several Word runs; they will still be"
            " replaced correctly."
        )

    structure = TemplateStructure(
        file_name=file_name,
        sha256=sha256,
        metadata=metadata,
        block_count=len(blocks),
        paragraph_count=sum(1 for b in blocks if b.kind == "p"),
        sections=sections,
        tables=tables,
        placeholders=list(placeholders.values()),
        headers_footers=headers_footers,
        page_setup=page_setup,
        styles_used=sorted({b.style_name for b in blocks if b.style_name}),
        image_count=image_count,
        hyperlink_count=hyperlink_count,
        has_numbered_lists=any(True for _ in body.iter(qn("w:numPr"))),
        warnings=warnings,
    )
    return structure, blocks


def parse_template(
    path: Path | str, display_name: str | None = None
) -> TemplateStructure:
    path = Path(path)
    data = path.read_bytes()
    doc = docx.Document(str(path))
    structure, _ = analyze_document(
        doc, display_name or path.name, hashlib.sha256(data).hexdigest()
    )
    return structure
