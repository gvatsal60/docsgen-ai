"""Map generated content to template targets (sections and placeholders).

Agents are asked to use the exact section ids (``s04``) and placeholder keys, but model output is
untrusted, so targets are resolved defensively: exact id, exact key, then title / key similarity.
"""

from __future__ import annotations

import difflib
import re

from models.agent_models import TargetType
from models.document_models import TemplateSection, TemplateStructure


def _norm(text: str) -> str:
    text = re.sub(
        r"^\s*(\d+(\.\d+)*[.)]?|chapter\s+\d+[:.]?)\s*",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def resolve_target(
    structure: TemplateStructure, target: str, target_type: str | None = None
) -> tuple[TargetType, str] | None:
    """Return (target_type, canonical_target) or None if the target does not exist in the template."""
    raw = (target or "").strip()
    if not raw:
        return None
    key = raw.strip("{} ")
    placeholder_keys = structure.placeholder_keys()
    section_ids = [s.section_id for s in structure.sections]
    if target_type != "placeholder" and raw.lower() in section_ids:
        return "section", raw.lower()
    if target_type != "section" and key in placeholder_keys:
        return "placeholder", key
    if target_type != "section":
        lowered = {k.lower(): k for k in placeholder_keys}
        if key.lower() in lowered:
            return "placeholder", lowered[key.lower()]
    if target_type != "placeholder":
        wanted = _norm(raw)
        best, score = None, 0.0
        for s in structure.sections:
            ratio = difflib.SequenceMatcher(None, wanted, _norm(s.title)).ratio()
            if ratio > score:
                best, score = s, ratio
        if best is not None and score >= 0.8:
            return "section", best.section_id
    return None


def fillable_sections(structure: TemplateStructure) -> list[TemplateSection]:
    """Sections that can receive generated content (have a heading)."""
    return [s for s in structure.sections if s.heading_block is not None]


def needs_content(section: TemplateSection) -> bool:
    """A section needs content when it is empty or still contains instructional text."""
    return not section.has_real_content or bool(section.instructional_blocks)


def default_targets(
    structure: TemplateStructure, include_sections_with_content: bool = False
) -> list[str]:
    targets = [f"{{{{{p.key}}}}}" for p in structure.placeholders]
    for s in fillable_sections(structure):
        if include_sections_with_content or needs_content(s):
            targets.append(s.section_id)
    return targets


def target_label(structure: TemplateStructure, target: str, target_type: str) -> str:
    if target_type == "placeholder":
        return f"{{{{{target}}}}}"
    section = structure.section(target)
    return f"{target} · {section.title}" if section else target
