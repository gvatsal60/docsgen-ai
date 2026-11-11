"""Compare the original template with a generated document (block-level text diff)."""

from __future__ import annotations

import difflib
from pathlib import Path

import docx

from document_engine.formatting_preserver import element_text, iter_body_blocks
from document_engine.placeholder_manager import document_parts
from models.document_models import BlockDiff, ComparisonReport


def document_blocks(path: Path | str, include_headers: bool = True) -> list[str]:
    doc = docx.Document(str(path))
    blocks = [element_text(el).strip() for el in iter_body_blocks(doc.element.body)]
    if include_headers:
        for partname, kind, root in document_parts(doc):
            if kind != "body":
                text = "\n".join(
                    element_text(p)
                    for p in root.iter(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
                    )
                ).strip()
                if text:
                    blocks.append(f"[{kind}] {text}")
    return [b for b in blocks if b]


def compare_documents(
    original: Path | str, generated: Path | str, max_diffs: int = 200
) -> ComparisonReport:
    a, b = document_blocks(original), document_blocks(generated)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    diffs: list[BlockDiff] = []
    added = removed = modified = unchanged = 0
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            unchanged += i2 - i1
            continue
        if op == "insert":
            added += j2 - j1
            diffs.extend(BlockDiff(change="added", generated=text) for text in b[j1:j2])
        elif op == "delete":
            removed += i2 - i1
            diffs.extend(
                BlockDiff(change="removed", original=text) for text in a[i1:i2]
            )
        else:
            left, right = a[i1:i2], b[j1:j2]
            for k in range(max(len(left), len(right))):
                lo = left[k] if k < len(left) else ""
                ro = right[k] if k < len(right) else ""
                if lo and ro:
                    modified += 1
                    ratio = difflib.SequenceMatcher(None, lo, ro).ratio()
                    diffs.append(
                        BlockDiff(
                            change="modified",
                            original=lo,
                            generated=ro,
                            similarity=round(ratio, 2),
                        )
                    )
                elif lo:
                    removed += 1
                    diffs.append(BlockDiff(change="removed", original=lo))
                else:
                    added += 1
                    diffs.append(BlockDiff(change="added", generated=ro))
    return ComparisonReport(
        original_blocks=len(a),
        generated_blocks=len(b),
        added=added,
        removed=removed,
        modified=modified,
        unchanged=unchanged,
        diffs=diffs[:max_diffs],
    )


def unified_text_diff(original: Path | str, generated: Path | str) -> str:
    a, b = document_blocks(original), document_blocks(generated)
    return "\n".join(
        difflib.unified_diff(
            a, b, fromfile="original", tofile="generated", lineterm="", n=1
        )
    )
