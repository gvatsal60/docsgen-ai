"""Find and replace ``{{placeholder}}`` markers anywhere in a Word document.

Word frequently splits typed text into several runs (spell-check, revision ids, formatting changes),
so ``{{company_name}}`` may be stored as ``{{comp`` + ``any_na`` + ``me}}``. Replacement works on the
concatenated text of a paragraph and maps offsets back to the individual ``w:t`` nodes, so:

* The replacement inherits the formatting of the run where the placeholder starts.
* Text before and after the placeholder keeps its own runs and formatting.
* Runs inside hyperlinks, fields and text boxes are handled; headers and footers are processed
  through their existing parts (no new parts are created).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from docx.document import Document as DocxDocument
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from lxml import etree

from document_engine.formatting_preserver import in_fallback, own_text_nodes

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.\-]{0,63})\s*\}\}")
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


@dataclass
class PlaceholderHit:
    key: str
    raw: str
    location: str
    split_across_runs: bool


@dataclass
class ReplaceResult:
    counts: dict[str, int] = field(default_factory=dict)
    touched_parts: set[str] = field(default_factory=set)

    def add(self, key: str, part: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1
        self.touched_parts.add(part)


def document_parts(doc: DocxDocument) -> list[tuple[str, str, etree._Element]]:
    """(partname, kind, root element) for the main body and every existing header/footer part."""
    parts = [(str(doc.part.partname).lstrip("/"), "body", doc.element.body)]
    seen = {id(doc.part)}
    for rel in doc.part.rels.values():
        if rel.is_external or rel.reltype not in (RT.HEADER, RT.FOOTER):
            continue
        part = rel.target_part
        if id(part) in seen:
            continue
        seen.add(id(part))
        kind = "header" if rel.reltype == RT.HEADER else "footer"
        parts.append((str(part.partname).lstrip("/"), kind, part.element))
    return parts


def _location(p: etree._Element, kind: str) -> str:
    if kind != "body":
        return kind
    for ancestor in p.iterancestors():
        if ancestor.tag == qn("w:txbxContent"):
            return "textbox"
        if ancestor.tag == qn("w:tc"):
            return "table"
    return "body"


def _paragraph_runs_text(
    p: etree._Element,
) -> tuple[str, list[tuple[etree._Element, int, int]]]:
    nodes = own_text_nodes(p)
    spans, pos, chunks = [], 0, []
    for t in nodes:
        text = t.text or ""
        spans.append((t, pos, pos + len(text)))
        chunks.append(text)
        pos += len(text)
    return "".join(chunks), spans


def find_placeholders(doc: DocxDocument) -> list[PlaceholderHit]:
    hits: list[PlaceholderHit] = []
    for _, kind, root in document_parts(doc):
        for p in root.iter(qn("w:p")):
            if in_fallback(p):
                continue
            text, spans = _paragraph_runs_text(p)
            if "{{" not in text:
                continue
            for m in PLACEHOLDER_RE.finditer(text):
                split = not any(
                    start <= m.start() and m.end() <= end for _, start, end in spans
                )
                hits.append(
                    PlaceholderHit(m.group(1), m.group(0), _location(p, kind), split)
                )
    return hits


def _set_text(t: etree._Element, text: str) -> None:
    t.text = text
    if text != text.strip() or "  " in text:
        t.set(XML_SPACE, "preserve")


def _write_value(t: etree._Element, before: str, value: str, after: str) -> None:
    """Put ``before + value + after`` into node ``t``; newlines in ``value`` become ``w:br`` in the same run."""
    lines = value.split("\n")
    if len(lines) == 1:
        _set_text(t, before + value + after)
        return
    run = t.getparent()
    _set_text(t, before + lines[0])
    anchor = t
    for i, line in enumerate(lines[1:], start=1):
        br = etree.Element(qn("w:br"))
        anchor.addnext(br)
        new_t = etree.Element(qn("w:t"))
        _set_text(new_t, line + (after if i == len(lines) - 1 else ""))
        br.addnext(new_t)
        anchor = new_t
    _ = run


def replace_in_paragraph(p: etree._Element, values: dict[str, str]) -> list[str]:
    """Replace placeholders in one paragraph. Returns the keys replaced (one entry per occurrence)."""
    text, spans = _paragraph_runs_text(p)
    if "{{" not in text:
        return []
    matches = [m for m in PLACEHOLDER_RE.finditer(text) if m.group(1) in values]
    replaced: list[str] = []
    for m in reversed(matches):  # right to left keeps earlier offsets valid
        start, end, key = m.start(), m.end(), m.group(1)
        value = values[key]
        first = next(i for i, (_, s, e) in enumerate(spans) if s <= start < e)
        last = next(i for i, (_, s, e) in enumerate(spans) if s < end <= e)
        t_first, s_first, _ = spans[first]
        t_last, s_last, _ = spans[last]
        before = (t_first.text or "")[: start - s_first]
        after = (t_last.text or "")[end - s_last :]
        if first == last:
            _write_value(t_first, before, value, after)
        else:
            _write_value(t_first, before, value, "")
            for i in range(first + 1, last):
                _set_text(spans[i][0], "")
            _set_text(t_last, after)
        replaced.append(key)
        text, spans = _paragraph_runs_text(p)
    _drop_empty_runs(p)
    return replaced


def _drop_empty_runs(p: etree._Element) -> None:
    for r in list(p.iter(qn("w:r"))):
        if next(r.iterancestors(qn("w:p")), None) is not p:
            continue
        children = [c for c in r if c.tag != qn("w:rPr")]
        if children and all(
            c.tag == qn("w:t") and not (c.text or "") for c in children
        ):
            r.getparent().remove(r)


def replace_placeholders(
    doc: DocxDocument, values: dict[str, str], parts: Iterable[str] | None = None
) -> ReplaceResult:
    """Replace placeholders in the body, tables, text boxes, headers and footers."""
    result = ReplaceResult()
    wanted = set(parts) if parts is not None else None
    for partname, _, root in document_parts(doc):
        if wanted is not None and partname not in wanted:
            continue
        for p in list(root.iter(qn("w:p"))):
            for key in replace_in_paragraph(p, values):
                # VML fallback copies are replaced too (so either rendering is correct) but not counted.
                if not in_fallback(p):
                    result.add(key, partname)
                else:
                    result.touched_parts.add(partname)
    return result


def remaining_placeholders(doc: DocxDocument) -> list[str]:
    return sorted({h.key for h in find_placeholders(doc)})
