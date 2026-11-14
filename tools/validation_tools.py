"""Deterministic validation helpers (numbers/dates against sources, duplicates, placeholders,
readability, text comparison) and their tool wrappers."""

from __future__ import annotations

import difflib
import json
import re

from strands import tool

from document_engine.placeholder_manager import PLACEHOLDER_RE
from models.agent_models import SectionDraft, ValidationIssue
from tools.context import DocumentContext
from tools.text_extraction import DATE_RE, MONEY_RE, NUMBER_RE, PERCENT_RE

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _normalize_number(value: str) -> str:
    digits = re.sub(r"[^\d.]", "", value)
    if digits.count(".") > 1:
        digits = digits.replace(".", "")
    return digits.rstrip("0").rstrip(".") if "." in digits else digits


def _source_numbers(ctx: DocumentContext, extra_text: str = "") -> set[str]:
    corpus = "\n".join(d.text for d in ctx.sources.values()) + "\n" + extra_text
    found = {_normalize_number(n) for n in NUMBER_RE.findall(corpus)}
    found |= {_normalize_number(m) for m in MONEY_RE.findall(corpus)}
    # thousands written as "248k" or "USD 248,000" -> both forms
    return {n for n in found if n}


def verify_values(
    text: str, ctx: DocumentContext, trusted_text: str = ""
) -> dict[str, list[str]]:
    """Check that numbers, amounts, percentages and dates in ``text`` appear in the sources or user answers."""
    corpus = (
        "\n".join(d.text for d in ctx.sources.values()) + "\n" + trusted_text
    ).lower()
    numbers = _source_numbers(ctx, trusted_text)
    supported: list[str] = []
    unsupported: list[str] = []
    candidates = set(MONEY_RE.findall(text)) | set(PERCENT_RE.findall(text))
    candidates |= {
        n for n in NUMBER_RE.findall(text) if len(_normalize_number(n)) >= 3 or "," in n
    }
    for value in sorted(candidates):
        norm = _normalize_number(value)
        ok = value.lower() in corpus or (norm and norm in numbers)
        (supported if ok else unsupported).append(value)
    for date in sorted(set(DATE_RE.findall(text))):
        (supported if date.lower() in corpus else unsupported).append(date)
    return {"supported": supported, "unsupported": unsupported}


def duplicate_sentences(
    texts: dict[str, str], min_len: int = 40
) -> list[tuple[str, list[str]]]:
    seen: dict[str, list[str]] = {}
    for target, text in texts.items():
        for sentence in _SENTENCE.split(text or ""):
            key = re.sub(r"\W+", " ", sentence.lower()).strip()
            if len(key) >= min_len:
                seen.setdefault(key, [])
                if target not in seen[key]:
                    seen[key].append(target)
    return [(s, targets) for s, targets in seen.items() if len(targets) > 1]


def readability(text: str) -> dict[str, float]:
    sentences = [s for s in _SENTENCE.split(text.strip()) if s.strip()]
    words = re.findall(r"[A-Za-z]+", text)
    if not sentences or not words:
        return {
            "sentences": 0,
            "words": 0,
            "avg_sentence_words": 0.0,
            "flesch_reading_ease": 0.0,
        }
    syllables = sum(max(1, len(re.findall(r"[aeiouy]+", w.lower()))) for w in words)
    asl = len(words) / len(sentences)
    asw = syllables / len(words)
    return {
        "sentences": len(sentences),
        "words": len(words),
        "avg_sentence_words": round(asl, 1),
        "flesch_reading_ease": round(206.835 - 1.015 * asl - 84.6 * asw, 1),
    }


def deterministic_checks(
    drafts: list[SectionDraft],
    ctx: DocumentContext,
    user_answers: dict[str, str] | None = None,
) -> list[ValidationIssue]:
    """Checks that do not need an LLM. Run before every final generation."""
    issues: list[ValidationIssue] = []
    trusted = "\n".join((user_answers or {}).values())
    active = [d for d in drafts if d.status.value in ("pending", "approved")]
    for d in active:
        content = d.content.strip()
        if not content:
            issues.append(
                ValidationIssue(
                    severity="error",
                    target=d.target,
                    category="missing",
                    message="Content is empty.",
                )
            )
            continue
        left = PLACEHOLDER_RE.findall(content)
        if left:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    target=d.target,
                    category="placeholder",
                    message=(
                        "Content still contains placeholders:"
                        f" {', '.join(sorted(set(left)))}."
                    ),
                )
            )
        if re.search(r"\[(?:insert|tbd|todo|missing)[^\]]*\]", content, re.IGNORECASE):
            issues.append(
                ValidationIssue(
                    severity="warning",
                    target=d.target,
                    category="missing",
                    message=(
                        "Content contains a gap marker such as [TBD] or [Insert …]."
                    ),
                )
            )
        values = verify_values(content, ctx, trusted)
        if values["unsupported"]:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    target=d.target,
                    category="unsupported",
                    message=(
                        "Values not found in the sources or your answers:"
                        f" {', '.join(values['unsupported'][:10])}."
                    ),
                    suggestion="Verify these values or remove them.",
                )
            )
    for sentence, targets in duplicate_sentences({d.target: d.content for d in active}):
        issues.append(
            ValidationIssue(
                severity="info",
                target=", ".join(targets),
                category="duplicate",
                message=f'Repeated sentence: "{sentence[:120]}…"',
            )
        )
    return issues


# ------------------------------------------------------------------------------- tools
def make_verify_values_in_sources(ctx: DocumentContext):
    @tool(name="verify_values_in_sources")
    def verify_values_in_sources(text: str) -> str:
        """Check whether the numbers, amounts, percentages and dates in a text appear in the uploaded sources.

        Args:
            text: The text to check.
        """
        return json.dumps(verify_values(text, ctx), indent=1)

    return verify_values_in_sources


def make_find_duplicate_content(ctx: DocumentContext):
    @tool(name="find_duplicate_content")
    def find_duplicate_content(sections_json: str) -> str:
        """Find sentences repeated across sections.

        Args:
            sections_json: JSON object mapping a section id or placeholder key to its text.
        """
        try:
            texts = json.loads(sections_json)
        except ValueError:
            texts = None
        if not isinstance(texts, dict):
            return "sections_json must be a JSON object of {target: text}."
        dups = duplicate_sentences({str(k): str(v) for k, v in texts.items()})
        return (
            json.dumps([{"sentence": s, "targets": t} for s, t in dups], indent=1)
            if dups
            else "No duplicated sentences found."
        )

    return find_duplicate_content


def make_check_placeholders(ctx: DocumentContext):
    @tool(name="check_remaining_placeholders")
    def check_remaining_placeholders(text: str) -> str:
        """List {{placeholders}} and gap markers ([TBD], [Insert …]) that remain in a text.

        Args:
            text: The text to check.
        """
        found = sorted(set(PLACEHOLDER_RE.findall(text)))
        gaps = re.findall(r"\[(?:insert|tbd|todo|missing)[^\]]*\]", text, re.IGNORECASE)
        return json.dumps({"placeholders": found, "gap_markers": gaps})

    return check_remaining_placeholders


def make_compare_texts(ctx: DocumentContext):
    @tool(name="compare_texts")
    def compare_texts(original: str, revised: str) -> str:
        """Compare two texts: similarity ratio and a compact unified diff.

        Args:
            original: The original text.
            revised: The revised text.
        """
        ratio = difflib.SequenceMatcher(None, original, revised).ratio()
        diff = "\n".join(
            difflib.unified_diff(
                original.splitlines(), revised.splitlines(), lineterm="", n=0
            )
        )
        return json.dumps(
            {"similarity": round(ratio, 3), "diff": diff[:6000]}, indent=1
        )

    return compare_texts


def make_readability_stats(ctx: DocumentContext):
    @tool(name="readability_stats")
    def readability_stats(text: str) -> str:
        """Readability statistics: sentence count, words, average sentence length and Flesch reading ease.

        Args:
            text: The text to analyze.
        """
        return json.dumps(readability(text))

    return readability_stats
