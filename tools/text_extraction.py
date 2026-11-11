"""Plain-text extraction, chunking, entity extraction and search across source documents."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from strands import tool

from models.document_models import (
    ExtractedEntities,
    SourceChunk,
    SourceDocument,
    SourceRef,
)
from services.security import wrap_untrusted
from tools.context import DocumentContext

CHUNK_CHARS = 1_200

_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"  # noqa: E501
DATE_RE = re.compile(
    rf"\b(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}[/.]\d{{1,2}}[/.]\d{{2,4}}|\d{{1,2}}\s+{_MONTHS}\s+\d{{4}}|{_MONTHS}\s+\d{{1,2}},?\s+\d{{4}}|{_MONTHS}\s+\d{{4}}|Q[1-4]\s+\d{{4}})\b",  # noqa: E501
    re.IGNORECASE,
)
MONEY_RE = re.compile(
    r"(?:(?:USD|EUR|GBP|INR|JPY|CHF|AUD|CAD)\s?|[$€£₹¥])\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|bn|million|billion|thousand|lakh|crore))?|\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|INR|dollars|euros|pounds|rupees)",  # noqa: E501
    re.IGNORECASE,
)
PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%")
NUMBER_RE = re.compile(
    r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w.])|(?<![\w.])\d+(?:\.\d+)?(?![\w.%])"
)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
URL_RE = re.compile(r"\bhttps?://[^\s<>()\"']+", re.IGNORECASE)
REF_RE = re.compile(
    r"\b(?:[A-Z]{2,6}-\d{2,6}(?:-\d{1,6})?|(?:Ref(?:erence)?|PO|Invoice|Contract)\s*(?:No\.?|#|:)\s*[\w\-/]+)\b"
)
NAME_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|[A-Z]\.|of|de|van|von|and|&)){1,4})\b"
)
_STOP_NAMES = {
    "The",
    "This",
    "That",
    "These",
    "Phase",
    "Data",
    "Project",
    "Section",
    "Table",
    "Page",
    "Date",
}


def _uniq(values: list[str], limit: int = 60) -> list[str]:
    seen, out = set(), []
    for v in values:
        v = v.strip().rstrip(".,;")
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
        if len(out) >= limit:
            break
    return out


def extract_entities(text: str) -> ExtractedEntities:
    money = _uniq(MONEY_RE.findall(text))
    numbers = [
        n
        for n in _uniq(NUMBER_RE.findall(text), 120)
        if not re.fullmatch(r"(19|20)\d{2}", n)
    ]
    names = [
        n for n in _uniq(NAME_RE.findall(text), 80) if n.split()[0] not in _STOP_NAMES
    ]
    return ExtractedEntities(
        dates=_uniq(DATE_RE.findall(text)),
        money=money,
        percentages=_uniq(PERCENT_RE.findall(text)),
        numbers=numbers[:60],
        emails=_uniq(EMAIL_RE.findall(text)),
        urls=_uniq(URL_RE.findall(text)),
        references=_uniq(REF_RE.findall(text)),
        candidate_names=names[:40],
    )


def chunk_lines(
    doc_id: str, file_name: str, text: str, unit: str = "lines"
) -> list[SourceChunk]:
    """Split text into ~CHUNK_CHARS chunks on line boundaries, recording the line range."""
    chunks: list[SourceChunk] = []
    lines = text.splitlines()
    buf: list[str] = []
    start = 1
    size = 0
    for i, line in enumerate(lines, start=1):
        if size + len(line) > CHUNK_CHARS and buf:
            chunks.append(
                _mk_chunk(
                    doc_id,
                    file_name,
                    len(chunks) + 1,
                    "\n".join(buf),
                    f"{unit} {start}-{i - 1}",
                )
            )
            buf, size, start = [], 0, i
        buf.append(line)
        size += len(line) + 1
    if any(b.strip() for b in buf):
        chunks.append(
            _mk_chunk(
                doc_id,
                file_name,
                len(chunks) + 1,
                "\n".join(buf),
                f"{unit} {start}-{len(lines)}",
            )
        )
    return chunks


def _mk_chunk(
    doc_id: str, file_name: str, n: int, text: str, location: str
) -> SourceChunk:
    cid = f"{doc_id}-C{n}"
    return SourceChunk(
        chunk_id=cid,
        doc_id=doc_id,
        text=text,
        ref=SourceRef(
            doc_id=doc_id, file_name=file_name, location=location, chunk_id=cid
        ),
    )


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_text_file(
    path: Path, doc_id: str, file_name: str, sha256: str
) -> SourceDocument:
    text = decode_text(path.read_bytes()).replace("\r\n", "\n")
    return SourceDocument(
        doc_id=doc_id,
        file_name=file_name,
        file_type="txt",
        size_bytes=path.stat().st_size,
        sha256=sha256,
        stored_path=str(path),
        text=text,
        chunks=chunk_lines(doc_id, file_name, text),
        entities=extract_entities(text),
        metadata={"lines": text.count("\n") + 1},
    )


# ------------------------------------------------------------------------------- search
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]+")


def search_chunks(
    ctx: DocumentContext, query: str, max_results: int = 6
) -> list[tuple[float, SourceChunk]]:
    """Small BM25 keyword search over all source chunks."""
    terms = [t.lower() for t in _WORD.findall(query) if len(t) > 1]
    chunks = ctx.all_chunks()
    if not terms or not chunks:
        return []
    tokenized = [Counter(t.lower() for t in _WORD.findall(c.text)) for c in chunks]
    avg_len = sum(sum(t.values()) for t in tokenized) / len(tokenized) or 1
    df = {term: sum(1 for t in tokenized if term in t) for term in set(terms)}
    scored = []
    for chunk, tf in zip(chunks, tokenized):
        length = sum(tf.values()) or 1
        score = 0.0
        for term in terms:
            if term not in tf:
                continue
            idf = math.log(1 + (len(chunks) - df[term] + 0.5) / (df[term] + 0.5))
            freq = tf[term]
            score += idf * freq * 2.2 / (freq + 1.2 * (0.25 + 0.75 * length / avg_len))
        if query.lower() in chunk.text.lower():
            score += 2.0
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:max_results]


# -------------------------------------------------------------------------------- tools
def make_list_source_documents(ctx: DocumentContext):
    @tool(name="list_source_documents")
    def list_source_documents() -> str:
        """List the uploaded source documents with their ids, types, sizes and number of text chunks."""
        docs = [
            {
                "doc_id": d.doc_id,
                "file_name": d.file_name,
                "type": d.file_type,
                "chars": len(d.text),
                "chunks": len(d.chunks),
                "pages": d.pages,
                "sheets": d.sheets,
            }
            for d in ctx.sources.values()
        ]
        return json.dumps(docs, indent=1)

    return list_source_documents


def make_read_source_document(ctx: DocumentContext):
    @tool(name="read_source_document")
    def read_source_document(
        doc_id: str, offset: int = 0, max_chars: int = 6000
    ) -> str:
        """Read the extracted text of an uploaded source document.

        Args:
            doc_id: Document id from list_source_documents (for example D1).
            offset: Character offset to start reading from.
            max_chars: Maximum number of characters to return (at most 12000).
        """
        doc = ctx.source(doc_id)
        max_chars = max(200, min(int(max_chars), 12_000))
        offset = max(0, int(offset))
        piece = doc.text[offset : offset + max_chars]
        more = len(doc.text) - (offset + len(piece))
        footer = (
            f"\n[{more} more characters; continue with offset={offset + len(piece)}]"
            if more > 0
            else ""
        )
        return ctx.limit(
            wrap_untrusted(
                f"{doc.file_name} chars {offset}-{offset + len(piece)}", piece
            )
            + footer
        )

    return read_source_document


def make_search_source_documents(ctx: DocumentContext):
    @tool(name="search_source_documents")
    def search_source_documents(query: str, max_results: int = 6) -> str:
        """Keyword search across all uploaded source documents. Returns matching chunks with chunk ids and locations.

        Args:
            query: Words or phrase to search for.
            max_results: Maximum number of chunks to return (1-15).
        """
        hits = search_chunks(ctx, query, max(1, min(int(max_results), 15)))
        if not hits:
            return "No matching passages found."
        parts = [
            wrap_untrusted(
                f"{c.chunk_id} | {c.ref.file_name} | {c.ref.location} | score {s:.2f}",
                c.text,
            )
            for s, c in hits
        ]
        return ctx.limit("\n\n".join(parts))

    return search_source_documents


def make_get_source_entities(ctx: DocumentContext):
    @tool(name="get_source_entities")
    def get_source_entities(doc_id: str) -> str:
        """Return entities found by pattern matching in a document.

        Entities are dates, monetary amounts, percentages, numbers, emails, URLs, references
        and candidate names.

        Args:
            doc_id: Document id from list_source_documents.
        """
        doc = ctx.source(doc_id)
        return ctx.limit(json.dumps(doc.entities.model_dump(), indent=1))

    return get_source_entities


def make_get_source_chunk(ctx: DocumentContext):
    @tool(name="get_source_chunk")
    def get_source_chunk(chunk_id: str) -> str:
        """Return the full text and location of one source chunk.

        Args:
            chunk_id: Chunk id such as D1-C3.
        """
        chunk = ctx.chunk(chunk_id)
        if chunk is None:
            return f"Unknown chunk id '{chunk_id}'."
        return ctx.limit(
            wrap_untrusted(
                f"{chunk.chunk_id} | {chunk.ref.file_name} | {chunk.ref.location}",
                chunk.text,
            )
        )

    return get_source_chunk
