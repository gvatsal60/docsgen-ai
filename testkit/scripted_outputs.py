"""Scripted agent outputs for the sample template and sample sources (used by agent/workflow tests)."""

from __future__ import annotations

import re

SECTIONS = {
    "s01": "Executive Summary",
    "s02": "Introduction",
    "s03": "Project Background",
    "s04": "Objectives",
    "s05": "Scope of Work",
    "s06": "Technical Approach",
    "s07": "Implementation Plan",
    "s08": "Budget",
    "s09": "Risk Assessment",
    "s10": "Conclusion",
}

ANALYSIS = {
    "document_type": "project proposal",
    "summary": "A ten-section project proposal with cover placeholders.",
    "sections": [
        {
            "section_id": sid,
            "title": title,
            "purpose": f"Describe the {title.lower()}.",
            "required_information": [title],
            "expected_length": "medium",
            "should_fill": True,
        }
        for sid, title in SECTIONS.items()
    ],
    "placeholders": [
        {"key": k, "description": k.replace("_", " "), "expected_type": "text"}
        for k in (
            "project_name",
            "client_name",
            "date",
            "prepared_by",
            "budget",
            "reference_number",
            "company_name",
            "scope_of_work",
        )
    ],
    "writing_style": "Formal, third person.",
    "notes": [],
}

EXTRACTION = {
    "facts": [
        {
            "fact_id": "F1",
            "statement": "The proposal is prepared by Northwind Analytics Ltd.",
            "value": "Northwind Analytics Ltd.",
            "category": "organization",
            "source_chunk_ids": ["D1-C1"],
            "confidence": "high",
        },
        {
            "fact_id": "F2",
            "statement": "The client is Contoso Retail Group.",
            "value": "Contoso Retail Group",
            "category": "organization",
            "source_chunk_ids": ["D1-C1"],
            "confidence": "high",
        },
        {
            "fact_id": "F3",
            "statement": "The total budget is USD 248,000.",
            "value": "USD 248,000",
            "category": "money",
            "source_chunk_ids": ["D1-C2"],
            "confidence": "high",
        },
        {
            "fact_id": "F4",
            "statement": "The project is the Unified Customer Data Platform.",
            "value": "Unified Customer Data Platform",
            "category": "other",
            "source_chunk_ids": ["D1-C1"],
            "confidence": "high",
        },
        {
            "fact_id": "F5",
            "statement": "Bogus fact with an invalid source.",
            "value": None,
            "category": "other",
            "source_chunk_ids": ["D99-C1"],
            "confidence": "high",
        },
    ],
    "mappings": [
        {
            "target": "budget",
            "target_type": "placeholder",
            "fact_ids": ["F3"],
            "notes": "",
        }
    ],
    "relationships": ["Budget line items sum to the total."],
    "conflicts": [],
    "missing": [
        {
            "target": "reference_number",
            "description": "No reference number in the sources",
            "critical": True,
            "question_for_user": "What is the proposal reference number?",
        }
    ],
}

RESEARCH = {
    "performed": True,
    "findings": [
        {
            "statement": "Northwind Analytics was founded in 2014.",
            "verified": True,
            "citation_title": "DocsGen demo directory",
            "citation_url": None,
            "tool_used": "demo_lookup_company",
            "related_target": "s02",
        }
    ],
    "assumptions": [],
    "limitations": [],
}

PLACEHOLDER_VALUES = {
    "project_name": "Unified Customer Data Platform",
    "client_name": "Contoso Retail Group",
    "date": "15 March 2026",
    "prepared_by": "Priya Raman",
    "budget": "USD 248,000",
    "company_name": "Northwind Analytics Ltd.",
    "scope_of_work": (
        "Data pipelines, a customer data model, a reporting layer and staff training."
    ),
}

SECTION_TEXT = {
    "s01": (
        "Northwind Analytics Ltd. proposes a **Unified Customer Data Platform** for"
        " Contoso Retail Group.\n\nThe total budget is USD 248,000."
    ),
    "s04": (
        "- Consolidate customer data from three systems.\n- Reduce campaign preparation"
        " time from 3 weeks to 2 days.\n- Achieve a data quality score of at least 95%."
    ),
    "s08": (
        "| Work package | Cost (USD) |\n|---|---|\n| Data pipelines | 96000 |\n|"
        " Training and rollout | 30000 |"
    ),
}


def _requested_targets(messages: list) -> list[str]:
    text = " ".join(
        block.get("text", "")
        for m in messages
        if m.get("role") == "user"
        for block in m.get("content", [])
        if isinstance(block, dict)
    )
    match = re.search(
        r"Generate content for exactly these targets: ([^\n]+?)\. Return", text
    )
    return [t.strip() for t in match.group(1).split(",")] if match else []


def generation(messages: list, tool_names: list[str]) -> dict:
    sections = []
    for target in _requested_targets(messages):
        if target in PLACEHOLDER_VALUES:
            sections.append(
                {
                    "target": target,
                    "target_type": "placeholder",
                    "content": PLACEHOLDER_VALUES[target],
                    "fact_ids": ["F1"] if target == "company_name" else [],
                }
            )
        elif target in SECTION_TEXT:
            sections.append(
                {
                    "target": target,
                    "target_type": "section",
                    "title": SECTIONS[target],
                    "content": SECTION_TEXT[target],
                    "fact_ids": ["F1", "F2", "F3"],
                }
            )
        elif target in SECTIONS:
            sections.append(
                {
                    "target": target,
                    "target_type": "section",
                    "title": SECTIONS[target],
                    "content": f"Content for {SECTIONS[target]} based on the sources.",
                    "fact_ids": ["F4"],
                }
            )
        # reference_number intentionally not generated (missing information)
    sections.append(
        {"target": "s99", "target_type": "section", "content": "hallucinated target"}
    )
    return {"sections": sections}


VALIDATION = {
    "complete": False,
    "completeness_score": 85,
    "issues": [
        {
            "severity": "warning",
            "target": "reference_number",
            "category": "missing",
            "message": "Reference number is missing.",
            "suggestion": "Ask the user.",
        }
    ],
    "summary": "Mostly complete.",
}

QUALITY = {
    "overall_score": 88,
    "grammar": "Good.",
    "readability": "Clear.",
    "consistency": "Consistent.",
    "formatting": "OK.",
    "revisions": [
        {
            "target": "s01",
            "revised_content": (
                "Northwind Analytics Ltd. proposes a Unified Customer Data Platform for"
                " Contoso Retail Group, with a total budget of USD 248,000."
            ),
            "rationale": "Merged sentences.",
        }
    ],
    "summary": "Good quality.",
}

OUTPUTS = {
    "TemplateAnalysis": ANALYSIS,
    "ExtractionResult": EXTRACTION,
    "ResearchResult": RESEARCH,
    "GenerationResult": generation,
    "ValidationReport": VALIDATION,
    "QualityReport": QUALITY,
}
