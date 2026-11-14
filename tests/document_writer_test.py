"""Writing approved content into the original template while preserving formatting."""

from __future__ import annotations

import zipfile

import docx
from docx.shared import Inches

from document_engine.document_comparator import compare_documents
from document_engine.document_writer import parse_content, write_document
from document_engine.template_parser import parse_template
from models.agent_models import DraftStatus, InsertMode, SectionDraft


def _draft(target, content, target_type="section", mode=InsertMode.AUTO):
    return SectionDraft(
        target=target,
        target_type=target_type,
        title=target,
        content=content,
        status=DraftStatus.APPROVED,
        insert_mode=mode,
    )


def _write(sample_template, tmp_path, drafts):
    out = tmp_path / "out.docx"
    report = write_document(sample_template, out, drafts)
    return out, report


def test_parse_content_blocks():
    blocks = parse_content(
        "Intro **bold**.\n\n- a\n- b\n\n1. one\n2. two\n\n| x | y |\n|---|---|\n| 1"
        " | 2 |"
    )
    assert [b.kind for b in blocks] == [
        "paragraph",
        "bullet",
        "bullet",
        "number",
        "number",
        "table",
    ]
    assert blocks[-1].rows == [["x", "y"], ["1", "2"]]


def test_sections_replace_instructions_and_keep_headings(sample_template, tmp_path):
    out, report = _write(
        sample_template,
        tmp_path,
        [
            _draft("s01", "Executive summary text."),
            _draft("s06", "Technical approach text."),
        ],
    )
    doc = docx.Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    i = texts.index("1. Executive Summary")
    assert texts[i + 1] == "Executive summary text."
    assert not any(t.startswith("[Insert a concise executive summary") for t in texts)
    assert "6. Technical Approach" in texts
    assert doc.paragraphs[i].style.name == "Heading 1"
    assert report.sections_written == ["s01", "s06"]


def test_existing_content_is_kept_and_new_content_appended(sample_template, tmp_path):
    out, _ = _write(
        sample_template, tmp_path, [_draft("s03", "Additional background.")]
    )
    texts = [p.text for p in docx.Document(str(out)).paragraphs]
    i = texts.index(
        "This proposal builds on the discovery workshops held earlier this year."
    )
    assert texts[i + 1] == "Additional background."


def test_replace_mode_removes_existing_body(sample_template, tmp_path):
    out, _ = _write(
        sample_template,
        tmp_path,
        [_draft("s03", "Fresh background.", mode=InsertMode.REPLACE)],
    )
    texts = [p.text for p in docx.Document(str(out)).paragraphs]
    assert "Fresh background." in texts
    assert (
        "This proposal builds on the discovery workshops held earlier this year."
        not in texts
    )


def test_lists_tables_and_inline_formatting(sample_template, tmp_path):
    content = (
        "Objectives:\n\n- First **key** goal\n- Second goal\n\n1. Step one\n2. Step two"
    )
    out, _ = _write(
        sample_template,
        tmp_path,
        [
            _draft("s04", content),
            _draft("s08", "| Item | Cost |\n|---|---|\n| A | 1 |"),
        ],
    )
    doc = docx.Document(str(out))
    bullets = [p for p in doc.paragraphs if p.text in ("First key goal", "Second goal")]
    assert bullets and all(p.style.name == "List Bullet" for p in bullets)
    assert any(r.bold and r.text == "key" for r in bullets[0].runs)
    numbered = [p for p in doc.paragraphs if p.text in ("Step one", "Step two")]
    assert numbered and all(p.style.name == "List Number" for p in numbered)
    assert len(doc.tables) == 3
    new_table = next(t for t in doc.tables if t.cell(0, 0).text == "Item")
    assert new_table.cell(1, 0).text == "A"
    assert any(
        r.bold for r in new_table.cell(0, 0).paragraphs[0].runs
    )  # header row bold
    # the new table sits where the instruction was: before the template's own budget table
    assert [t.cell(0, 0).text for t in doc.tables].index("Item") == 1
    assert (
        new_table.style.name == doc.tables[0].style.name
    )  # styled like the template's first table


def test_formatting_preservation_checks(sample_template, tmp_path):
    drafts = [
        _draft("s01", "Summary."),
        _draft("company_name", "Northwind Analytics Ltd.", "placeholder"),
        _draft("prepared_by", "Priya Raman", "placeholder"),
    ]
    out, report = _write(sample_template, tmp_path, drafts)
    checks = {c.check: c.status for c in report.format_checks}
    assert checks["Styles (fonts, sizes, colors, paragraph styles)"] == "preserved"
    assert (
        checks["Page size, orientation and margins (section properties)"] == "preserved"
    )
    assert checks["Section breaks"] == "preserved"
    assert checks["Images and logos"] == "preserved"
    assert checks["Hyperlinks"] == "preserved"
    assert checks["Table borders, widths and styles"] == "preserved"
    assert checks["Header part header1.xml"] == "changed_expected"
    assert "changed" not in checks.values()
    doc = docx.Document(str(out))
    assert doc.sections[1].orientation == 1  # landscape budget section kept
    assert doc.sections[0].left_margin == Inches(1.1)
    with zipfile.ZipFile(sample_template) as a, zipfile.ZipFile(out) as b:
        assert a.read("word/styles.xml") == b.read("word/styles.xml")
        assert sorted(n for n in a.namelist() if n.startswith("word/media")) == sorted(
            n for n in b.namelist() if n.startswith("word/media")
        )
    # body hyperlink + header
    assert report.placeholders_replaced["company_name"] == 2


def test_untouched_paragraphs_are_identical(sample_template, tmp_path):
    original = docx.Document(str(sample_template))
    out, _ = _write(sample_template, tmp_path, [_draft("s10", "Conclusion text.")])
    generated = docx.Document(str(out))
    before = {
        p.text: p._p.xml
        for p in original.paragraphs
        if p.text.startswith("3. Project Background")
        or p.text.startswith("This proposal builds")
    }
    after = {p.text: p._p.xml for p in generated.paragraphs if p.text in before}
    assert before == after


def test_comparison_report(sample_template, tmp_path):
    out, _ = _write(
        sample_template,
        tmp_path,
        [_draft("s01", "Summary paragraph one.\n\nSummary paragraph two.")],
    )
    cmp = compare_documents(sample_template, out)
    assert cmp.modified + cmp.added >= 2
    assert cmp.unchanged > 10


def test_reparse_generated_document(sample_template, tmp_path):
    out, _ = _write(
        sample_template,
        tmp_path,
        [_draft("s01", "Summary."), _draft("client_name", "Contoso", "placeholder")],
    )
    structure = parse_template(out)
    assert "client_name" not in structure.placeholder_keys()
    assert not structure.section("s01").is_empty


def test_template_xml_parser_does_not_resolve_external_entities(tmp_path):
    """Uploaded templates are untrusted: an XXE payload must not pull in local files."""
    from lxml import etree

    from document_engine.formatting_preserver import _SAFE_XML_PARSER

    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-MARKER", encoding="utf-8")
    payload = (
        '<?xml version="1.0"?>'
        f'<!DOCTYPE d [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>'
        "<d><t>&xxe;</t></d>"
    ).encode()
    root = etree.fromstring(payload, _SAFE_XML_PARSER)
    assert "TOP-SECRET-MARKER" not in etree.tostring(root).decode()
