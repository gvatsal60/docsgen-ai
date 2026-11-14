"""Document context shared by built-in tools.

Built-in tools only operate on documents uploaded in the current session (addressed by id), never on
arbitrary filesystem paths, so agents cannot read files outside the user's uploads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from models.document_models import SourceChunk, SourceDocument, TemplateStructure


@dataclass
class DocumentContext:
    template_path: Path | None = None
    template: TemplateStructure | None = None
    sources: dict[str, SourceDocument] = field(default_factory=dict)
    max_result_chars: int = 12_000

    def source(self, doc_id: str) -> SourceDocument:
        doc = self.sources.get((doc_id or "").strip())
        if doc is None:
            known = ", ".join(self.sources) or "none"
            raise KeyError(f"Unknown document id '{doc_id}'. Known ids: {known}")
        return doc

    def chunk(self, chunk_id: str) -> SourceChunk | None:
        doc_id = chunk_id.split("-")[0] if chunk_id else ""
        doc = self.sources.get(doc_id)
        if doc is None:
            return None
        return next((c for c in doc.chunks if c.chunk_id == chunk_id), None)

    def all_chunks(self) -> list[SourceChunk]:
        return [c for d in self.sources.values() for c in d.chunks]

    def limit(self, text: str) -> str:
        if len(text) <= self.max_result_chars:
            return text
        return (
            text[: self.max_result_chars]
            + f"\n…[truncated; {len(text) - self.max_result_chars} more characters]"
        )
