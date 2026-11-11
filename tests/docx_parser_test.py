"""Template parsing: headings, sections, tables, placeholders, headers/footers, metadata, page setup."""

from __future__ import annotations

import docx
from docx.shared import Pt

from document_engine.template_parser import is_instructional, parse_template


def test_sections_and_headings(sample_template):
    s = parse_template(sample_template, "sample_template.docx")
    titles = [sec.title for sec in s.sections if sec.heading_block is not None]
    assert titles[0] == "1. Executive Summary"
    assert titles[-1] == "10. Conclusion"
    assert len(titles) == 10
    # cover/preamble with the Title paragraph
    assert s.sections[0].section_id == "s00"
    exec_summary = s.section("s01")
    assert exec_summary.is_empty and exec_summary.instructional_blocks
    background = s.section("s03")
    assert background.has_real_content and not background.instructional_blocks


def test_placeholders_locations_and_split_runs(sample_template):
    s = parse_template(sample_template)
    by_key = {p.key: p for p in s.placeholders}
    assert {
        "project_name",
        "client_name",
        "date",
        "prepared_by",
        "budget",
        "reference_number",
        "company_name",
        "scope_of_work",
    } <= set(by_key)
    assert by_key["client_name"].split_across_runs
    assert by_key[
        "company_name"
    ].split_across_runs  # inside a hyperlink, split across runs
    assert "header" in by_key["company_name"].locations
    assert "footer" in by_key["prepared_by"].locations
    assert "table" in by_key["reference_number"].locations


def test_tables_headers_footers_layout_metadata(sample_template):
    s = parse_template(sample_template)
    assert len(s.tables) == 2
    budget_table = next(t for t in s.tables if t.section_id == "s08")
    assert budget_table.preview[0] == ["Work package", "Cost", "Notes"]
    header = next(h for h in s.headers_footers if h.kind == "header")
    assert header.has_images and "company_name" in header.placeholders
    assert [p.orientation for p in s.page_setup] == [
        "portrait",
        "landscape",
        "portrait",
    ]
    assert s.page_setup[0].margins_in["left"] == 1.1
    assert s.metadata["title"] == "Project Proposal Template"
    assert s.image_count == 1 and s.hyperlink_count == 1


def test_instruction_detection():
    assert is_instructional("[Insert the executive summary here]")
    assert is_instructional("Describe the purpose of this document.")
    assert is_instructional("This section should explain the architecture.")
    assert not is_instructional("Northwind will deliver the platform in 2026.")


def test_fallback_headings_without_styles(tmp_path):
    doc = docx.Document()
    for title in ("INTRODUCTION", "METHODOLOGY"):
        p = doc.add_paragraph()
        run = p.add_run(title)
        run.bold = True
        run.font.size = Pt(14)
        doc.add_paragraph(
            "Body text for the section, which is long enough to be body text."
        )
    path = tmp_path / "plain.docx"
    doc.save(path)
    s = parse_template(path)
    assert [x.title for x in s.sections if x.heading_block is not None] == [
        "INTRODUCTION",
        "METHODOLOGY",
    ]
