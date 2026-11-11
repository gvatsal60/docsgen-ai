"""Placeholder replacement across runs, hyperlinks, tables, headers, footers and text boxes."""

from __future__ import annotations

import docx
from docx.shared import Pt, RGBColor

from document_engine.placeholder_manager import (
    find_placeholders,
    remaining_placeholders,
    replace_placeholders,
)


def _doc_with_split_placeholder():
    doc = docx.Document()
    p = doc.add_paragraph()
    p.add_run("Hello ")
    r1 = p.add_run("{{na")
    r1.bold = True
    r1.font.color.rgb = RGBColor(0xC0, 0, 0)
    r2 = p.add_run("me}}")
    r2.bold = True
    p.add_run(", welcome to {{place}}!")
    return doc, p


def test_split_runs_keep_formatting_and_surrounding_text():
    doc, p = _doc_with_split_placeholder()
    result = replace_placeholders(doc, {"name": "Ada", "place": "London"})
    assert p.text == "Hello Ada, welcome to London!"
    bold_runs = [r for r in p.runs if r.bold]
    assert bold_runs and bold_runs[0].text == "Ada"
    assert bold_runs[0].font.color.rgb == RGBColor(0xC0, 0, 0)
    assert result.counts == {"name": 1, "place": 1}


def test_unknown_placeholders_are_left_untouched():
    doc, p = _doc_with_split_placeholder()
    replace_placeholders(doc, {"place": "Paris"})
    assert "{{name}}" in p.text
    assert remaining_placeholders(doc) == ["name"]


def test_multiline_value_becomes_line_breaks():
    doc = docx.Document()
    p = doc.add_paragraph("Address: {{address}} (end)")
    replace_placeholders(doc, {"address": "1 Main St\nSpringfield"})
    xml = p._p.xml
    assert "<w:br/>" in xml
    assert p.text.replace("\n", "|") == "Address: 1 Main St|Springfield (end)"


def test_multiple_placeholders_same_run_and_whitespace():
    doc = docx.Document()
    p = doc.add_paragraph("{{ a }}-{{b}}-{{a}}")
    p.runs[0].font.size = Pt(15)
    replace_placeholders(doc, {"a": "1", "b": "2"})
    assert p.text == "1-2-1"
    assert p.runs[0].font.size == Pt(15)


def test_headers_footers_tables_and_hyperlinks(sample_template):
    doc = docx.Document(str(sample_template))
    keys = {h.key for h in find_placeholders(doc)}
    assert "company_name" in keys
    replace_placeholders(
        doc, {"company_name": "ACME", "prepared_by": "Jo", "reference_number": "R-1"}
    )
    header_text = "".join(p.text for p in doc.sections[0].header.paragraphs)
    footer_text = "".join(p.text for p in doc.sections[0].footer.paragraphs)
    assert "ACME — Confidential" in header_text
    assert "Prepared by Jo" in footer_text
    assert doc.tables[0].cell(2, 1).text == "R-1"
    body_xml = doc.element.body.xml
    assert (
        "ACME" in body_xml and "w:hyperlink" in body_xml
    )  # hyperlink element kept around replaced text


def test_text_box_placeholders(tmp_path):
    # Build a paragraph containing a DrawingML text box with a placeholder.
    from docx.oxml import parse_xml

    doc = docx.Document()
    p = doc.add_paragraph()
    nsdecl = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        ' xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
    )
    run = parse_xml(f"<w:r {nsdecl}><w:t>Box: </w:t></w:r>")
    box = parse_xml(
        "<w:r"
        f" {nsdecl}><w:pict><w:txbxContent><w:p><w:r><w:t>{{{{boxed}}}}</w:t></w:r></w:p></w:txbxContent></w:pict></w:r>"  # noqa: E501
    )
    p._p.append(run)
    p._p.append(box)
    hits = find_placeholders(doc)
    assert [(h.key, h.location) for h in hits] == [("boxed", "textbox")]
    replace_placeholders(doc, {"boxed": "inside"})
    assert "inside" in doc.element.body.xml
