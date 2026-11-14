"""Low-level OOXML helpers that keep the template's formatting intact.

* Block iteration and text extraction that understand runs, hyperlinks, text boxes and fields.
* Cloning paragraph/run properties from a reference paragraph so new content inherits the template's
  fonts, sizes, colors, spacing, indentation and styles.
* Snapshots of formatting-relevant parts (styles, numbering, section properties, headers/footers,
  images, hyperlinks, tables) and a comparison used to verify preservation after generation.
"""

from __future__ import annotations

import copy
import hashlib
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from docx.oxml.ns import qn
from lxml import etree

from models.document_models import FormatCheck

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {
    "w": W_NS,
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "v": "urn:schemas-microsoft-com:vml",
}
_RSID_ATTR = re.compile(r'\s+w:rsid\w*="[^"]*"')


# ----------------------------------------------------------------------------- traversal
# Uploaded templates are untrusted: never resolve entities or fetch anything over the network.
_SAFE_XML_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, huge_tree=False
)


def iter_body_blocks(body: etree._Element) -> Iterator[etree._Element]:
    """Yield top-level block elements (paragraphs, tables, content controls) in document order."""
    for child in body.iterchildren():
        if child.tag in (qn("w:p"), qn("w:tbl"), qn("w:sdt")):
            yield child


def own_text_nodes(p: etree._Element) -> list[etree._Element]:
    """``w:t`` elements that belong to paragraph ``p`` (not to nested text-box paragraphs)."""
    nodes = []
    for t in p.iter(qn("w:t")):
        parent_p = next(t.iterancestors(qn("w:p")), None)
        if parent_p is p:
            nodes.append(t)
    return nodes


def paragraph_text(p: etree._Element) -> str:
    """Visible text of a paragraph including tabs and line breaks, excluding nested text boxes."""
    parts: list[str] = []
    for node in p.iter(qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr")):
        if next(node.iterancestors(qn("w:p")), None) is not p:
            continue
        if node.tag == qn("w:t"):
            parts.append(node.text or "")
        elif node.tag == qn("w:tab"):
            # w:tab inside pPr/tabs is a tab stop definition, not content
            if node.getparent() is not None and node.getparent().tag == qn("w:r"):
                parts.append("\t")
        elif node.tag == qn("w:br"):
            if node.get(qn("w:type")) not in ("page", "column"):
                parts.append("\n")
        else:
            parts.append("\n")
    return "".join(parts)


def element_text(el: etree._Element) -> str:
    """Text of any block (paragraph, table or content control)."""
    if el.tag == qn("w:p"):
        return paragraph_text(el)
    rows = []
    for p in el.iter(qn("w:p")):
        if _in_fallback(p):
            continue
        rows.append(paragraph_text(p))
    return "\n".join(r for r in rows if r.strip())


def _in_fallback(el: etree._Element) -> bool:
    return any(a.tag == f"{{{NS['mc']}}}Fallback" for a in el.iterancestors())


def in_fallback(el: etree._Element) -> bool:
    """True for VML fallback copies of text boxes (duplicates of the DrawingML content)."""
    return _in_fallback(el)


def has_page_break(p: etree._Element) -> bool:
    if p.find("w:pPr/w:pageBreakBefore", NS) is not None:
        return True
    return any(br.get(qn("w:type")) == "page" for br in p.iter(qn("w:br")))


def has_section_break(p: etree._Element) -> bool:
    return p.find("w:pPr/w:sectPr", NS) is not None


def has_drawing(el: etree._Element) -> bool:
    return any(True for _ in el.iter(qn("w:drawing"), qn("w:pict"), qn("w:object")))


def has_field(el: etree._Element) -> bool:
    return any(True for _ in el.iter(qn("w:fldSimple"), qn("w:fldChar")))


def is_structural(p: etree._Element) -> bool:
    """Paragraphs that carry layout (breaks, images, fields) and must never be removed."""
    return has_section_break(p) or has_page_break(p) or has_drawing(p) or has_field(p)


# ----------------------------------------------------------------------------- cloning
_RUN_PROPS_TO_DROP = ("w:highlight", "w:vanish", "w:shd")


def clean_paragraph_properties(
    ppr: etree._Element | None, keep_numbering: bool = False
) -> etree._Element | None:
    if ppr is None:
        return None
    ppr = copy.deepcopy(ppr)
    # A section break lives in the pPr of the last paragraph of a section: never duplicate it.
    for el in ppr.findall("w:sectPr", NS):
        ppr.remove(el)
    if not keep_numbering:
        for el in ppr.findall("w:numPr", NS):
            ppr.remove(el)
    for el in ppr.findall("w:pageBreakBefore", NS):
        ppr.remove(el)
    return ppr


def reference_run_properties(
    p: etree._Element | None, strip_instruction_styling: bool = False
) -> etree._Element | None:
    """Run properties (font, size, color, ...) of the first text run of a reference paragraph."""
    if p is None:
        return None
    for r in p.iter(qn("w:r")):
        if next(r.iterancestors(qn("w:p")), None) is not p:
            continue
        if r.find("w:t", NS) is None:
            continue
        rpr = r.find("w:rPr", NS)
        if rpr is None:
            return None
        rpr = copy.deepcopy(rpr)
        drop = list(_RUN_PROPS_TO_DROP)
        if strip_instruction_styling:
            drop += ["w:i", "w:iCs", "w:color"]
        for tag in drop:
            for el in rpr.findall(tag, NS):
                rpr.remove(el)
        return rpr
    return None


def new_paragraph(ppr: etree._Element | None) -> etree._Element:
    p = etree.Element(qn("w:p"))
    if ppr is not None:
        p.append(copy.deepcopy(ppr))
    return p


def add_text_run(
    p: etree._Element,
    text: str,
    rpr: etree._Element | None,
    *,
    bold: bool = False,
    italic: bool = False,
) -> None:
    """Append runs for ``text`` (newlines become line breaks) with the given run properties."""
    run = etree.SubElement(p, qn("w:r"))
    props = copy.deepcopy(rpr) if rpr is not None else None
    if bold or italic:
        if props is None:
            props = etree.Element(qn("w:rPr"))
        for tag, flag in (("w:b", bold), ("w:i", italic)):
            if flag and props.find(tag, NS) is None:
                _insert_rpr_child(props, etree.Element(qn(tag)))
    if props is not None and len(props):
        run.append(props)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i:
            etree.SubElement(run, qn("w:br"))
        t = etree.SubElement(run, qn("w:t"))
        t.text = line
        if line != line.strip() or "  " in line:
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


# Order of rPr children required by the schema (subset sufficient for b/i insertion).
_RPR_ORDER = [
    "rStyle",
    "rFonts",
    "b",
    "bCs",
    "i",
    "iCs",
    "caps",
    "smallCaps",
    "strike",
    "dstrike",
    "outline",
    "shadow",
    "emboss",
    "imprint",
    "noProof",
    "snapToGrid",
    "vanish",
    "webHidden",
    "color",
    "spacing",
    "w",
    "kern",
    "position",
    "sz",
    "szCs",
    "highlight",
    "u",
    "effect",
    "bdr",
    "shd",
    "fitText",
    "vertAlign",
    "rtl",
    "cs",
    "em",
    "lang",
    "eastAsianLayout",
    "specVanish",
    "oMath",
]


def _insert_rpr_child(rpr: etree._Element, child: etree._Element) -> None:
    name = etree.QName(child).localname
    order = _RPR_ORDER.index(name) if name in _RPR_ORDER else len(_RPR_ORDER)
    for i, existing in enumerate(rpr):
        ename = etree.QName(existing).localname
        if ename in _RPR_ORDER and _RPR_ORDER.index(ename) > order:
            rpr.insert(i, child)
            return
    rpr.append(child)


# ----------------------------------------------------------------------------- snapshots
@dataclass
class FormattingSnapshot:
    part_hashes: dict[str, str] = field(default_factory=dict)
    section_props: list[str] = field(default_factory=list)
    image_hashes: list[str] = field(default_factory=list)
    hyperlinks: int = 0
    page_breaks: int = 0
    tables: list[str] = field(default_factory=list)
    styles_used: set[str] = field(default_factory=set)
    fonts_used: set[str] = field(default_factory=set)
    list_paragraphs: int = 0
    drawings: int = 0


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _canonical(el: etree._Element) -> str:
    return _RSID_ATTR.sub("", etree.tostring(el, encoding="unicode"))


def snapshot(path: Path | str) -> FormattingSnapshot:
    snap = FormattingSnapshot()
    header_footer_roots = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.startswith("word/media/"):
                snap.image_hashes.append(_hash(zf.read(name)))
            elif name in (
                "word/styles.xml",
                "word/numbering.xml",
                "word/theme/theme1.xml",
                "word/fontTable.xml",
                "word/settings.xml",
            ) or re.match(r"word/(header|footer)\d*\.xml$", name):
                data = zf.read(name)
                snap.part_hashes[name] = _hash(data)
                if "header" in name or "footer" in name:
                    header_footer_roots.append(etree.fromstring(data, _SAFE_XML_PARSER))
        body = etree.fromstring(zf.read("word/document.xml"), _SAFE_XML_PARSER)
    for root in header_footer_roots:
        snap.drawings += sum(1 for _ in root.iter(qn("w:drawing"), qn("w:pict")))
        snap.hyperlinks += sum(1 for _ in root.iter(qn("w:hyperlink")))
    snap.section_props = [_canonical(s) for s in body.iter(qn("w:sectPr"))]
    snap.hyperlinks += sum(1 for _ in body.iter(qn("w:hyperlink")))
    snap.page_breaks = sum(
        1 for br in body.iter(qn("w:br")) if br.get(qn("w:type")) == "page"
    ) + sum(1 for _ in body.iter(qn("w:pageBreakBefore")))
    for tbl in body.iter(qn("w:tbl")):
        tblpr = tbl.find("w:tblPr", NS)
        grid = tbl.find("w:tblGrid", NS)
        snap.tables.append(
            _hash(
                (
                    (_canonical(tblpr) if tblpr is not None else "")
                    + (_canonical(grid) if grid is not None else "")
                ).encode()
            )
        )
    for st in body.iter(qn("w:pStyle"), qn("w:rStyle"), qn("w:tblStyle")):
        snap.styles_used.add(st.get(qn("w:val")) or "")
    for rf in body.iter(qn("w:rFonts")):
        for attr in ("w:ascii", "w:hAnsi"):
            if rf.get(qn(attr)):
                snap.fonts_used.add(rf.get(qn(attr)))
    snap.list_paragraphs = sum(1 for _ in body.iter(qn("w:numPr")))
    snap.drawings += sum(1 for _ in body.iter(qn("w:drawing"), qn("w:pict")))
    return snap


def compare_snapshots(
    before: FormattingSnapshot,
    after: FormattingSnapshot,
    *,
    changed_parts_expected: set[str] | None = None,
) -> list[FormatCheck]:
    """Compare two snapshots and describe what was preserved."""
    expected = changed_parts_expected or set()
    checks: list[FormatCheck] = []

    labels = {
        "word/styles.xml": "Styles (fonts, sizes, colors, paragraph styles)",
        "word/numbering.xml": "List numbering definitions",
        "word/theme/theme1.xml": "Theme",
        "word/fontTable.xml": "Font table",
        "word/settings.xml": "Document settings",
    }
    for part, digest in sorted(before.part_hashes.items()):
        label = labels.get(
            part,
            f"{'Header' if 'header' in part else 'Footer'} part {part.split('/')[-1]}",
        )
        other = after.part_hashes.get(part)
        if other is None:
            checks.append(
                FormatCheck(
                    check=label,
                    status="changed",
                    detail="Part is missing in the generated document.",
                )
            )
        elif other == digest:
            checks.append(FormatCheck(check=label, status="preserved"))
        elif part in expected:
            checks.append(
                FormatCheck(
                    check=label,
                    status="changed_expected",
                    detail=(
                        "Updated only where placeholders or list numbering were filled."
                    ),
                )
            )
        else:
            checks.append(
                FormatCheck(
                    check=label, status="changed", detail="Part content differs."
                )
            )

    checks.append(
        FormatCheck(
            check="Page size, orientation and margins (section properties)",
            status=(
                "preserved"
                if before.section_props == after.section_props
                else "changed"
            ),
            detail=f"{len(before.section_props)} section(s)",
        )
    )
    checks.append(
        FormatCheck(
            check="Section breaks",
            status=(
                "preserved"
                if len(before.section_props) == len(after.section_props)
                else "changed"
            ),
            detail=f"{len(before.section_props)} → {len(after.section_props)}",
        )
    )
    checks.append(
        FormatCheck(
            check="Images and logos",
            status=(
                "preserved"
                if sorted(before.image_hashes) == sorted(after.image_hashes)
                and after.drawings >= before.drawings
                else "changed"
            ),
            detail=(
                f"{len(before.image_hashes)} image part(s), {before.drawings} →"
                f" {after.drawings} placement(s)"
            ),
        )
    )
    checks.append(
        FormatCheck(
            check="Page breaks",
            status=(
                "preserved" if after.page_breaks >= before.page_breaks else "changed"
            ),
            detail=f"{before.page_breaks} → {after.page_breaks}",
        )
    )
    checks.append(
        FormatCheck(
            check="Hyperlinks",
            status="preserved" if after.hyperlinks >= before.hyperlinks else "changed",
            detail=f"{before.hyperlinks} → {after.hyperlinks}",
        )
    )
    remaining = list(after.tables)
    missing_tables = 0
    for t in before.tables:
        if t in remaining:
            remaining.remove(t)
        else:
            missing_tables += 1
    checks.append(
        FormatCheck(
            check="Table borders, widths and styles",
            status="preserved" if missing_tables == 0 else "changed",
            detail=f"{len(before.tables)} original table(s); {len(remaining)} added",
        )
    )
    lost_styles = before.styles_used - after.styles_used
    checks.append(
        FormatCheck(
            check="Paragraph and character styles in use",
            status="preserved" if not lost_styles else "changed_expected",
            detail=(
                "No styles lost."
                if not lost_styles
                else (
                    "No longer used (replaced instructional text):"
                    f" {', '.join(sorted(lost_styles))}"
                )
            ),
        )
    )
    lost_fonts = before.fonts_used - after.fonts_used
    checks.append(
        FormatCheck(
            check="Direct font formatting",
            status="preserved" if not lost_fonts else "changed_expected",
            detail=(
                "All fonts still in use."
                if not lost_fonts
                else f"No longer used: {', '.join(sorted(lost_fonts))}"
            ),
        )
    )
    checks.append(
        FormatCheck(
            check="Numbered and bulleted lists",
            status=(
                "preserved"
                if after.list_paragraphs >= before.list_paragraphs
                else "changed_expected"
            ),
            detail=(
                f"{before.list_paragraphs} → {after.list_paragraphs} list paragraph(s)"
            ),
        )
    )
    return checks
