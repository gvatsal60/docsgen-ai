"""Populate the original DOCX template with approved content.

The template file is opened (never recreated). Only the paragraphs that receive content are
modified; every other part (styles, numbering, headers, footers, images, section properties) is left
byte-for-byte as it was unless a placeholder inside it is replaced.

Content format accepted from drafts (simple and safe, no HTML):

* Paragraphs separated by blank lines; single newlines become line breaks.
* ``- item`` / ``* item`` → bulleted list, ``1. item`` → numbered list (using the template's own list
  formatting when it has lists, otherwise the "List Bullet"/"List Number" styles, otherwise a text prefix).
* ``**bold**`` and ``*italic*`` inline.
* Pipe tables (``| a | b |`` with a ``|---|`` separator row) → a Word table styled like the template's first table.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path

import docx
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from lxml import etree

from document_engine.formatting_preserver import (
    NS,
    add_text_run,
    clean_paragraph_properties,
    compare_snapshots,
    is_structural,
    new_paragraph,
    reference_run_properties,
    snapshot,
)
from document_engine.placeholder_manager import (
    remaining_placeholders,
    replace_placeholders,
)
from document_engine.template_parser import Block, analyze_document
from models.agent_models import InsertMode, SectionDraft
from models.document_models import WriteReport

_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*\s][^*]*\*)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


# ------------------------------------------------------------------------------- parsing
@dataclass
class ContentBlock:
    kind: str  # paragraph | bullet | number | table
    text: str = ""
    rows: list[list[str]] | None = None


def parse_content(content: str) -> list[ContentBlock]:
    blocks: list[ContentBlock] = []
    lines = (content or "").replace("\r\n", "\n").split("\n")
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            text = "\n".join(buffer).strip()
            if text:
                blocks.append(ContentBlock("paragraph", text))
            buffer.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if (
            _TABLE_ROW.match(line)
            and i + 1 < len(lines)
            and _TABLE_SEP.match(lines[i + 1])
        ):
            flush()
            rows = [[c.strip() for c in line.strip().strip("|").split("|")]]
            i += 2
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            blocks.append(ContentBlock("table", rows=rows))
            continue
        m_b, m_n = _BULLET.match(line), _NUMBERED.match(line)
        if m_b:
            flush()
            blocks.append(ContentBlock("bullet", m_b.group(1).strip()))
        elif m_n:
            flush()
            blocks.append(ContentBlock("number", m_n.group(1).strip()))
        elif not line.strip():
            flush()
        else:
            buffer.append(line.rstrip())
        i += 1
    flush()
    return blocks


def _inline_runs(text: str) -> list[tuple[str, bool, bool]]:
    parts = []
    for piece in _INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            parts.append((piece[2:-2], True, False))
        elif piece.startswith("*") and piece.endswith("*") and len(piece) > 2:
            parts.append((piece[1:-1], False, True))
        else:
            parts.append((piece, False, False))
    return parts


# ----------------------------------------------------------------------------- lists
class ListFormatter:
    """Find list formatting in the template and create list paragraphs that use it."""

    def __init__(self, doc: DocxDocument) -> None:
        self.doc = doc
        self.numbering = None
        try:
            self.numbering = doc.part.numbering_part.element
        except (KeyError, NotImplementedError, AttributeError):
            self.numbering = None
        self.bullet_ppr = self._find_list_ppr("bullet")
        self.number_ppr = self._find_list_ppr("number")
        self.style_ids = {
            s.name: s.style_id for s in doc.styles if s.type == 1
        }  # paragraph styles
        self.used_numbering = False
        self._restart_num_id: str | None = None

    def _num_format(self, num_id: str, ilvl: str) -> str | None:
        if self.numbering is None:
            return None
        num = self.numbering.find(f"w:num[@w:numId='{num_id}']", NS)
        if num is None:
            return None
        abstract = num.find("w:abstractNumId", NS)
        if abstract is None:
            return None
        aid = abstract.get(qn("w:val"))
        lvl = self.numbering.find(
            f"w:abstractNum[@w:abstractNumId='{aid}']/w:lvl[@w:ilvl='{ilvl}']", NS
        )
        fmt = lvl.find("w:numFmt", NS) if lvl is not None else None
        return fmt.get(qn("w:val")) if fmt is not None else None

    def _find_list_ppr(self, kind: str) -> etree._Element | None:
        for p in self.doc.element.body.iter(qn("w:p")):
            ppr = p.find("w:pPr", NS)
            num_pr = ppr.find("w:numPr", NS) if ppr is not None else None
            if num_pr is None:
                continue
            num_id_el, ilvl_el = num_pr.find("w:numId", NS), num_pr.find("w:ilvl", NS)
            if num_id_el is None:
                continue
            fmt = self._num_format(
                num_id_el.get(qn("w:val")),
                ilvl_el.get(qn("w:val")) if ilvl_el is not None else "0",
            )
            if fmt is None or fmt == "none":
                continue
            if (kind == "bullet") == (fmt == "bullet"):
                return clean_paragraph_properties(ppr, keep_numbering=True)
        return None

    def _restarted_number_ppr(self) -> etree._Element | None:
        """Numbered lists restart at 1 by creating a new numbering instance of the same definition."""
        if self.number_ppr is None or self.numbering is None:
            return self.number_ppr
        if self._restart_num_id is None:
            num_id_el = self.number_ppr.find("w:numPr/w:numId", NS)
            src = self.numbering.find(
                f"w:num[@w:numId='{num_id_el.get(qn('w:val'))}']", NS
            )
            if src is None:
                return self.number_ppr
            existing = [
                int(n.get(qn("w:numId"))) for n in self.numbering.findall("w:num", NS)
            ]
            new_id = str(max(existing) + 1)
            new_num = etree.SubElement(self.numbering, qn("w:num"))
            new_num.set(qn("w:numId"), new_id)
            abstract = etree.SubElement(new_num, qn("w:abstractNumId"))
            abstract.set(qn("w:val"), src.find("w:abstractNumId", NS).get(qn("w:val")))
            override = etree.SubElement(new_num, qn("w:lvlOverride"))
            override.set(qn("w:ilvl"), "0")
            start = etree.SubElement(override, qn("w:startOverride"))
            start.set(qn("w:val"), "1")
            self._restart_num_id = new_id
            self.used_numbering = True
        ppr = copy.deepcopy(self.number_ppr)
        ppr.find("w:numPr/w:numId", NS).set(qn("w:val"), self._restart_num_id)
        return ppr

    def start_numbered_list(self) -> None:
        self._restart_num_id = None

    def paragraph_properties(
        self, kind: str, base_ppr: etree._Element | None
    ) -> tuple[etree._Element | None, bool]:
        """Return (pPr, needs_text_prefix) for a list item.

        Preference: the template's own list formatting, then the built-in list styles, then the body
        paragraph formatting with a typed bullet/number prefix.
        """
        if kind == "bullet" and self.bullet_ppr is not None:
            return copy.deepcopy(self.bullet_ppr), False
        if kind == "number" and self.number_ppr is not None:
            return self._restarted_number_ppr(), False
        style_name = "List Bullet" if kind == "bullet" else "List Number"
        if style_name in self.style_ids:
            ppr = etree.Element(qn("w:pPr"))
            ps = etree.SubElement(ppr, qn("w:pStyle"))
            ps.set(qn("w:val"), self.style_ids[style_name])
            return ppr, False
        return (copy.deepcopy(base_ppr) if base_ppr is not None else None), True


# ------------------------------------------------------------------------------- writer
class DocumentWriter:
    def __init__(self, template_path: Path | str) -> None:
        self.template_path = Path(template_path)
        self.doc = docx.Document(str(self.template_path))
        self.structure, self.blocks = analyze_document(
            self.doc, self.template_path.name, ""
        )
        self.lists = ListFormatter(self.doc)
        self.report = WriteReport()
        self._first_table = next(
            (b.element for b in self.blocks if b.kind == "tbl"), None
        )

    # .................................................................. rendering helpers
    def _render(
        self, content: str, base_ppr: etree._Element | None, rpr: etree._Element | None
    ) -> list[etree._Element]:
        elements: list[etree._Element] = []
        previous_kind = None
        number_counter = 0
        for block in parse_content(content):
            if block.kind == "table":
                elements.append(self._make_table(block.rows or [], rpr))
                previous_kind = "table"
                continue
            if block.kind == "number" and previous_kind != "number":
                self.lists.start_numbered_list()
                number_counter = 0
            if block.kind in ("bullet", "number"):
                ppr, fallback = self.lists.paragraph_properties(block.kind, base_ppr)
                if block.kind == "number":
                    number_counter += 1
                prefix = (
                    ("• " if block.kind == "bullet" else f"{number_counter}. ")
                    if fallback
                    else ""
                )
                p = new_paragraph(ppr)
                text = prefix + block.text
            else:
                p = new_paragraph(base_ppr)
                text = block.text
            for piece, bold, italic in _inline_runs(text):
                add_text_run(p, piece, rpr, bold=bold, italic=italic)
            elements.append(p)
            previous_kind = block.kind
        return elements

    def _make_table(
        self, rows: list[list[str]], rpr: etree._Element | None
    ) -> etree._Element:
        cols = max(len(r) for r in rows) if rows else 1
        table = self.doc.add_table(rows=len(rows), cols=cols)
        tbl = table._tbl
        if self._first_table is not None:
            src_pr = self._first_table.find("w:tblPr", NS)
            if src_pr is not None:
                tbl.remove(tbl.find("w:tblPr", NS))
                tbl.insert(0, copy.deepcopy(src_pr))
        else:
            try:
                table.style = self.doc.styles["Table Grid"]
            except KeyError:
                pass
        for r_idx, row in enumerate(rows):
            for c_idx in range(cols):
                cell = table.cell(r_idx, c_idx)
                p = cell.paragraphs[0]._p
                text = row[c_idx] if c_idx < len(row) else ""
                for piece, bold, italic in _inline_runs(text):
                    add_text_run(p, piece, rpr, bold=bold or r_idx == 0, italic=italic)
        tbl.getparent().remove(
            tbl
        )  # detach from the end of the body; inserted by the caller
        return tbl

    # .................................................................. sections
    def _reference_paragraph(
        self, section_blocks: list[Block], heading: Block
    ) -> tuple[etree._Element | None, bool]:
        """Pick the paragraph whose formatting new body text should inherit."""
        for b in section_blocks:
            if (
                b.kind == "p"
                and b.text.strip()
                and not b.instructional
                and b.heading_level is None
            ):
                return b.element, False
        for b in section_blocks:
            if b.kind == "p" and b.instructional:
                return b.element, True
        for b in section_blocks:
            if b.kind == "p" and b.empty and not is_structural(b.element):
                return b.element, False
        start = heading.index + 1
        for b in self.blocks[start:]:
            if (
                b.kind == "p"
                and b.text.strip()
                and b.heading_level is None
                and not b.instructional
            ):
                return b.element, False
        return None, False

    def write_section(self, draft: SectionDraft) -> None:
        section = self.structure.section(draft.target)
        if section is None or section.heading_block is None:
            self.report.sections_skipped.append(draft.target)
            self.report.warnings.append(
                f"Section {draft.target} no longer exists in the template; skipped."
            )
            return
        heading = self.blocks[section.heading_block]
        body = [self.blocks[i] for i in section.body_blocks]
        ref_p, from_instruction = self._reference_paragraph(body, heading)
        base_ppr = (
            clean_paragraph_properties(ref_p.find("w:pPr", NS))
            if ref_p is not None
            else None
        )
        rpr = reference_run_properties(
            ref_p, strip_instruction_styling=from_instruction
        )
        new_elements = self._render(draft.content, base_ppr, rpr)
        if not new_elements:
            self.report.sections_skipped.append(draft.target)
            return

        mode = draft.insert_mode
        instructions = [
            b
            for b in body
            if b.kind == "p" and b.instructional and not is_structural(b.element)
        ]
        empties = [
            b
            for b in body
            if b.kind == "p" and b.empty and not is_structural(b.element)
        ]

        # Decide what to remove and where to insert.
        #  AUTO    – instructional text (and empty paragraphs of an otherwise empty section) is replaced in place;
        #            sections with real content and no instructions get the new content appended.
        #  REPLACE – all non-structural body content is replaced.
        #  APPEND  – existing content is kept, stale instructional text is removed, content goes at the end.
        removable: list[Block]
        position: str
        if mode == InsertMode.REPLACE:
            removable = [
                b
                for b in body
                if (b.kind == "p" and not is_structural(b.element))
                or b.kind in ("tbl", "sdt")
            ]
            position = "first_removed"
        elif mode == InsertMode.APPEND:
            removable = instructions
            position = "after_content"
        else:
            removable = instructions + (empties if not section.has_real_content else [])
            position = (
                "first_removed"
                if instructions or not section.has_real_content
                else "after_content"
            )
        removable = sorted(
            {b.index: b for b in removable}.values(), key=lambda b: b.index
        )
        removed_ids = {b.index for b in removable}

        anchor = heading.element
        keep = [b for b in body if b.index not in removed_ids]
        if position == "first_removed" and removable:
            prev = removable[0].element.getprevious()
            anchor = prev if prev is not None else heading.element
        elif keep:
            # After the last content block, but before trailing page/section-break paragraphs.
            trailing = list(keep)
            while (
                trailing
                and trailing[-1].kind == "p"
                and is_structural(trailing[-1].element)
                and not trailing[-1].text.strip()
            ):
                trailing.pop()
            if trailing:
                anchor = trailing[-1].element
        for b in removable:
            parent = b.element.getparent()
            if parent is not None:
                parent.remove(b.element)
        for el in new_elements:
            anchor.addnext(el)
            anchor = el
        self.report.sections_written.append(draft.target)

    # .................................................................. main entry
    def apply(
        self, drafts: list[SectionDraft], extra_values: dict[str, str] | None = None
    ) -> None:
        values: dict[str, str] = dict(extra_values or {})
        section_drafts: list[SectionDraft] = []
        for d in drafts:
            if d.target_type == "placeholder":
                values[d.target] = d.content.strip()
            else:
                section_drafts.append(d)
        # Sections are written bottom-up so earlier block indices stay valid.
        order = {s.section_id: i for i, s in enumerate(self.structure.sections)}
        for d in sorted(
            section_drafts, key=lambda x: order.get(x.target, -1), reverse=True
        ):
            self.write_section(d)
        result = replace_placeholders(self.doc, values)
        self.report.placeholders_replaced = result.counts
        self._touched_parts = result.touched_parts
        if self.lists.used_numbering:
            self._touched_parts.add("word/numbering.xml")
        self.report.placeholders_remaining = remaining_placeholders(self.doc)
        self.report.sections_written.reverse()

    def save(self, out_path: Path | str) -> WriteReport:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(str(out_path))
        before = snapshot(self.template_path)
        after = snapshot(out_path)
        self.report.format_checks = compare_snapshots(
            before, after, changed_parts_expected=getattr(self, "_touched_parts", set())
        )
        return self.report


def write_document(
    template_path: Path | str,
    out_path: Path | str,
    drafts: list[SectionDraft],
    extra_values: dict[str, str] | None = None,
) -> WriteReport:
    writer = DocumentWriter(template_path)
    writer.apply(drafts, extra_values)
    return writer.save(out_path)
