"""Generate the sample template and source documents in ``sample_data/``.

Run:  python scripts/make_samples.py

The template deliberately contains the features DocsGen must preserve: a logo in the header, a page
number field in the footer, placeholders split across runs and inside a hyperlink/table/header/footer,
custom fonts and colors, a styled table, bulleted and numbered lists, page breaks, a landscape
section, and instructional text in empty sections.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import docx
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


OUT = ROOT / "sample_data"


def make_png(width: int = 240, height: int = 64) -> bytes:
    """A small two-color logo PNG written without third-party imaging libraries."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            if x < height:  # square emblem
                row += (
                    bytes((254, 200, 4))
                    if (x - 32) ** 2 + (y - 32) ** 2 < 26**2
                    else bytes((1, 48, 92))
                )
            else:
                band = (x // 12) % 2
                row += (
                    bytes((1, 48, 92))
                    if band or y < 8 or y > height - 8
                    else bytes((20, 80, 140))
                )
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def add_hyperlink(paragraph, url: str, parts: list[str]) -> None:
    """Add a hyperlink whose text is split across several runs (as Word often stores it)."""
    part = paragraph.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    for text in parts:
        run = OxmlElement("w:r")
        rpr = OxmlElement("w:rPr")
        style = OxmlElement("w:rStyle")
        style.set(qn("w:val"), "Hyperlink")
        color = OxmlElement("w:color")
        color.set(qn("w:val"), "0563C1")
        underline = OxmlElement("w:u")
        underline.set(qn("w:val"), "single")
        rpr.extend([style, color, underline])
        run.append(rpr)
        t = OxmlElement("w:t")
        t.text = text
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        run.append(t)
        link.append(run)
    paragraph._p.append(link)


def add_page_field(paragraph) -> None:
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "1"
    run.append(t)
    fld.append(run)
    paragraph._p.append(fld)


def instruction(doc, text: str) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.italic = True
    run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)


def build_template(path: Path) -> None:
    doc = docx.Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    for level, color in (
        (1, RGBColor(0x01, 0x30, 0x5C)),
        (2, RGBColor(0x2E, 0x75, 0xB6)),
    ):
        style = doc.styles[f"Heading {level}"]
        style.font.name = "Georgia"
        style.font.color.rgb = color

    section = doc.sections[0]
    section.top_margin = section.bottom_margin = Inches(1)
    section.left_margin = section.right_margin = Inches(1.1)

    # Header: logo + placeholder; footer: placeholder + PAGE field
    logo_path = OUT / "logo.png"
    logo_path.write_bytes(make_png())
    header_p = section.header.paragraphs[0]
    header_p.add_run().add_picture(str(logo_path), width=Inches(1.2))
    header_p.add_run("    {{company_name}} — Confidential").font.size = Pt(9)
    footer_p = section.footer.paragraphs[0]
    footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer_p.add_run("Prepared by {{prepared_by}} · Page ").font.size = Pt(9)
    add_page_field(footer_p)

    # Cover
    doc.add_paragraph("{{project_name}}", style="Title")
    p = doc.add_paragraph()
    p.add_run("Project proposal for ")
    p.add_run(
        "{{cli"
    ).bold = True  # placeholder split across runs with different formatting
    p.add_run("ent_name}}").bold = True
    fancy = doc.add_paragraph()
    run = fancy.add_run("Date: {{date}}")
    run.font.name = "Georgia"
    run.font.color.rgb = RGBColor(0xA6, 0x7C, 0x00)

    table = doc.add_table(rows=3, cols=2)
    table.style = doc.styles["Light Grid Accent 1"]
    for row, (label, value) in zip(
        table.rows,
        [
            ("Prepared by", "{{prepared_by}}"),
            ("Total budget", "{{budget}}"),
            ("Reference", "{{reference_number}}"),
        ],
    ):
        row.cells[0].text = label
        row.cells[1].text = value
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    doc.add_heading("1. Executive Summary", level=1)
    instruction(
        doc,
        "[Insert a concise executive summary of the proposal: the problem, the"
        " solution, cost and timeline.]",
    )

    doc.add_heading("2. Introduction", level=1)
    p = doc.add_paragraph("More information is available on the ")
    add_hyperlink(p, "https://example.com/about", ["{{company", "_name}} website"])
    p.add_run(".")
    instruction(
        doc, "Describe the purpose of this document and the organizations involved."
    )

    doc.add_heading("3. Project Background", level=1)
    doc.add_paragraph(
        "This proposal builds on the discovery workshops held earlier this year."
    )

    doc.add_heading("4. Objectives", level=1)
    instruction(doc, "List the measurable objectives of the project.")
    doc.add_paragraph("Example objective format", style="List Bullet")

    doc.add_heading("5. Scope of Work", level=1)
    doc.add_paragraph("{{scope_of_work}}")

    doc.add_heading("6. Technical Approach", level=1)
    instruction(
        doc,
        "This section should explain the architecture, technologies and methods used.",
    )

    doc.add_heading("7. Implementation Plan", level=1)
    doc.add_paragraph("Phases are delivered in the following order:")
    doc.add_paragraph("Discovery", style="List Number")
    doc.add_paragraph("Build", style="List Number")
    instruction(doc, "[Add the detailed timeline with milestones.]")

    # Landscape section for the budget
    landscape = doc.add_section(WD_SECTION.NEW_PAGE)
    landscape.orientation = WD_ORIENT.LANDSCAPE
    landscape.page_width, landscape.page_height = (
        landscape.page_height,
        landscape.page_width,
    )
    doc.add_heading("8. Budget", level=1)
    instruction(
        doc, "Summarize the budget by work package. The total must match {{budget}}."
    )
    budget = doc.add_table(rows=2, cols=3)
    budget.style = doc.styles["Table Grid"]
    for cell, text in zip(budget.rows[0].cells, ["Work package", "Cost", "Notes"]):
        cell.text = text
        cell.paragraphs[0].runs[0].bold = True
    portrait = doc.add_section(WD_SECTION.NEW_PAGE)
    portrait.orientation = WD_ORIENT.PORTRAIT
    portrait.page_width, portrait.page_height = (
        portrait.page_height,
        portrait.page_width,
    )

    doc.add_heading("9. Risk Assessment", level=1)
    instruction(doc, "Identify the main risks, their likelihood and mitigation.")

    doc.add_heading("10. Conclusion", level=1)
    instruction(doc, "Summarize why the proposal should be accepted.")

    doc.core_properties.title = "Project Proposal Template"
    doc.core_properties.author = "DocsGen sample"
    doc.save(str(path))


SOURCE_TXT = """\
PROJECT PROPOSAL NOTES — Northwind Analytics Ltd.
Prepared by: Priya Raman, Head of Delivery
Client: Contoso Retail Group
Project: Unified Customer Data Platform
Reference: NWA-2026-014
Date of proposal: 15 March 2026

Background
Contoso Retail Group operates 42 stores and an online shop. Customer data is spread across a point-of-sale
system, an e-commerce platform and a loyalty application. Discovery workshops in January 2026 found that
marketing campaigns take 3 weeks to prepare because data must be merged manually.

Objectives
- Consolidate customer data from the three systems into a single platform.
- Reduce campaign preparation time from 3 weeks to 2 days.
- Achieve a data quality score of at least 95% for customer records.

Scope of work
Northwind Analytics will design and implement data pipelines, a customer data model, a reporting layer and
staff training. Hardware procurement and changes to the point-of-sale software are out of scope.

Technical approach
The platform will run on Amazon Web Services using managed services: Amazon S3 for storage, AWS Glue for
data pipelines and Amazon Redshift for analytics. Data will be encrypted at rest and in transit.

Timeline
Phase 1 Discovery: April 2026. Phase 2 Build: May to July 2026. Phase 3 Rollout and training: August 2026.

Risks
Data quality in the legacy loyalty system is unknown. Mitigation: profile the data in Phase 1.
Staff availability during the summer may delay training. Mitigation: schedule training sessions early.

Budget
The total budget is USD 248,000 (see the budget spreadsheet for the breakdown).
"""

BUDGET_ROWS = [
    ("Work package", "Cost (USD)", "Notes"),
    ("Discovery and data profiling", "28000", "4 weeks"),
    ("Data pipelines", "96000", "AWS Glue, 3 source systems"),
    ("Data model and reporting", "74000", "Redshift + dashboards"),
    ("Training and rollout", "30000", "6 sessions"),
    ("Contingency", "20000", "~8%"),
]


def build_sources() -> None:
    (OUT / "sample_source.txt").write_text(SOURCE_TXT, encoding="utf-8")
    with open(OUT / "sample_budget.csv", "w", encoding="utf-8", newline="") as fh:
        import csv

        csv.writer(fh).writerows(BUDGET_ROWS)

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Budget"
    for row in BUDGET_ROWS:
        ws.append([int(v) if v.isdigit() else v for v in row])
    ws.append(["Total", "=SUM(B2:B6)", ""])
    ws2 = wb.create_sheet("Team")
    ws2.append(["Name", "Role"])
    ws2.append(["Priya Raman", "Head of Delivery"])
    ws2.append(["Tom Becker", "Lead Data Engineer"])
    wb.save(OUT / "sample_budget.xlsx")

    import fitz  # PyMuPDF

    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text(
        (72, 72),
        "Technical Specification — Unified Customer Data Platform",
        fontsize=14,
    )
    body = (
        "1. Data sources: point-of-sale (nightly export), e-commerce platform (REST"
        " API), loyalty app (database).\n2. Availability target: 99.5% monthly for the"
        " reporting layer.\n3. Security: encryption at rest (AES-256) and in transit"
        " (TLS 1.2+). Access via single sign-on.\n4. Retention: raw data kept for 24"
        " months."
    )
    page.insert_textbox(fitz.Rect(72, 100, 540, 400), body, fontsize=11)
    page2 = pdf.new_page()
    page2.insert_text((72, 72), "Appendix: Service levels", fontsize=12)
    page2.insert_textbox(
        fitz.Rect(72, 100, 540, 300),
        "Incident response within 4 business hours.\nSupport contract: 12 months after"
        " go-live.",
        fontsize=11,
    )
    pdf.save(OUT / "sample_specification.pdf")

    profile = docx.Document()
    profile.add_heading("Northwind Analytics Ltd. — Company Profile", level=1)
    profile.add_paragraph(
        "Founded in 2014 in Manchester, Northwind Analytics has 85 employees and"
        " specializes in data platforms for retail and logistics."
    )
    profile.add_paragraph("Contact: delivery@northwind.example")
    profile.save(OUT / "sample_company_profile.docx")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    build_template(OUT / "sample_template.docx")
    build_sources()
    print("Samples written to", OUT)
