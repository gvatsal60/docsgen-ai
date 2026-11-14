"""Source extraction (TXT, CSV, XLSX, PDF, DOCX), traceability, search and upload validation."""

from __future__ import annotations

import io
import zipfile

import pytest

from services.file_service import (
    ALLOWED_SOURCE_TYPES,
    UploadRejected,
    sniff_and_validate,
)
from tests.conftest import SAMPLES, load_sources
from tools.text_extraction import extract_entities, search_chunks


def test_all_formats_extract_with_references(container):
    load_sources(container)
    docs = {d.file_type: d for d in container.session.sources.values()}
    assert set(docs) == {"txt", "csv", "pdf", "xlsx", "docx"}
    txt = docs["txt"]
    assert "Contoso Retail Group" in txt.text and txt.chunks[0].ref.location.startswith(
        "lines 1-"
    )
    assert "USD 248,000" in txt.entities.money
    pdf = docs["pdf"]
    assert pdf.pages == 2 and pdf.chunks[0].ref.location == "page 1"
    assert any("99.5%" in c.text for c in pdf.chunks)
    xlsx = docs["xlsx"]
    assert xlsx.sheets == ["Budget", "Team"]
    budget = next(t for t in xlsx.tables if t.name == "Budget")
    assert budget.columns[:2] == ["Work package", "Cost (USD)"]
    assert "sheet 'Budget' rows 2-" in xlsx.chunks[0].ref.location
    csv = docs["csv"]
    assert csv.tables[0].total_rows == 5
    profile = docs["docx"]
    assert (
        "Founded in 2014" in profile.text
        and "section 'Northwind Analytics" in profile.chunks[0].ref.location
    )


def test_entities():
    e = extract_entities(
        "Kick-off on 15 March 2026; budget USD 248,000 (8%). Contact a.b@example.com,"
        " Ref: NWA-2026-014. https://x.example/y"
    )
    assert "15 March 2026" in e.dates
    assert "USD 248,000" in e.money and "8%" in e.percentages
    assert e.emails == ["a.b@example.com"] and e.urls == ["https://x.example/y"]
    assert any("NWA-2026-014" in r for r in e.references)


def test_search(container):
    load_sources(container, names=("sample_source.txt", "sample_specification.pdf"))
    hits = search_chunks(container.document_context(), "availability target reporting")
    assert hits and hits[0][1].ref.file_name == "sample_specification.pdf"


@pytest.mark.parametrize(
    "name,data,message",
    [
        ("evil.exe", b"MZ", "Unsupported file type"),
        ("fake.pdf", b"hello", "not a valid PDF"),
        ("fake.docx", b"PK\x03\x04garbage", "not a valid Word"),
        ("bin.txt", b"abc\x00def", "binary data"),
        ("empty.csv", b"", "empty"),
    ],
)
def test_upload_validation_rejects(name, data, message):
    with pytest.raises(UploadRejected, match=message):
        sniff_and_validate(name, data, ALLOWED_SOURCE_TYPES, 10 * 1024 * 1024)


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for k, v in entries.items():
            zf.writestr(k, v)
    return buf.getvalue()


def test_macro_and_traversal_and_bomb_rejected():
    base = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<doc/>"}
    with pytest.raises(UploadRejected, match="Macro"):
        sniff_and_validate(
            "m.docx", _zip(base | {"word/vbaProject.bin": b"x"}), {"docx"}, 10**8
        )
    with pytest.raises(UploadRejected, match="unsafe"):
        sniff_and_validate(
            "t.docx", _zip(base | {"../evil.txt": b"x"}), {"docx"}, 10**8
        )
    with pytest.raises(UploadRejected, match="ZIP bomb|too large"):
        sniff_and_validate(
            "b.docx",
            _zip(base | {"word/big.xml": b"0" * (20 * 1024 * 1024)}),
            {"docx"},
            10**8,
        )


def test_size_limit():
    with pytest.raises(UploadRejected, match="limit"):
        sniff_and_validate("a.txt", b"x" * 2048, {"txt"}, 1024)


def test_valid_samples_accepted():
    for name in (
        "sample_template.docx",
        "sample_budget.xlsx",
        "sample_specification.pdf",
        "sample_source.txt",
    ):
        assert (
            sniff_and_validate(
                name, (SAMPLES / name).read_bytes(), ALLOWED_SOURCE_TYPES, 10**8
            )
            == name.rsplit(".", 1)[1]
        )
