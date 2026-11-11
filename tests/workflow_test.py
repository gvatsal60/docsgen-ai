"""End-to-end workflow with the real Strands loop, a scripted model, real MCP research and the real DOCX writer."""

from __future__ import annotations

import json
from pathlib import Path

import docx
import pytest

from models.agent_models import DraftStatus, TaskType, WorkflowStage
from services.workflow_service import WorkflowError, WorkflowService
from testkit import scripted_outputs as so
from testkit.helpers import ScriptedModel, use_model
from tests.conftest import load_sources, load_template


@pytest.fixture()
def ready(container, demo_stdio_config):
    load_template(container)
    load_sources(container)
    container.mcp.save_server(demo_stdio_config, persist=False, is_new=True)
    container.mcp.connect("demo")
    container.tools.set_permission(
        __import__(
            "models.agent_models", fromlist=["AgentRole"]
        ).AgentRole.RESEARCH_AGENT,
        "demo_lookup_company",
        True,
    )
    model = use_model(
        container,
        ScriptedModel(
            so.OUTPUTS,
            tool_calls={
                "ExtractionResult": [
                    {
                        "name": "search_source_documents",
                        "input": {"query": "budget total"},
                    }
                ],
                "ResearchResult": [
                    {
                        "name": "demo_lookup_company",
                        "input": {"name": "Northwind Analytics"},
                    }
                ],
                "ValidationReport": [
                    {
                        "name": "verify_values_in_sources",
                        "input": {"text": "USD 248,000"},
                    }
                ],
            },
        ),
    )
    return container, model


def test_complete_workflow_end_to_end(ready, tmp_path):
    c, _model = ready
    state = WorkflowService.new_state(
        TaskType.GENERATE_COMPLETE,
        "Fill the template. Do not fabricate.",
        research_enabled=True,
        research_topics="Company background",
    )
    progress = []
    c.workflow.run_pipeline(
        state, c.document_context(), lambda step, msg: progress.append(step)
    )
    assert state.stage == WorkflowStage.REVIEWED
    assert [r.role.value for r in state.agent_runs] == [
        "document_analyzer",
        "information_extractor",
        "research_agent",
        "document_generator",
        "document_generator",
        "document_generator",
        "document_validator",
        "quality_reviewer",
    ]
    assert state.agent_runs[2].tool_calls == [
        "demo_lookup_company"
    ]  # real MCP research call

    # facts get traceable sources; invalid chunk ids are dropped
    facts = {f.fact_id: f for f in state.extraction.facts}
    assert (
        facts["F3"].sources[0].file_name == "sample_source.txt"
        and "248,000" in facts["F3"].sources[0].excerpt
    )
    assert facts["F5"].sources == [] and facts["F5"].confidence == "low"

    # drafts: hallucinated target ignored, missing value not invented
    assert "s99" not in state.drafts and "reference_number" not in state.drafts
    assert state.drafts["s01"].sources and state.drafts["s01"].revision_suggestion
    assert any("s99" in e.message for e in state.events if e.level == "warning")

    # human review: answer the missing question, edit, reject one, approve the rest
    WorkflowService.record_answer(
        state,
        "reference_number",
        "What is the proposal reference number?",
        "NWA-2026-014",
        c.session.template.structure,
    )
    assert state.drafts["reference_number"].content == "NWA-2026-014"
    WorkflowService.apply_revision(state, "s01")
    state.drafts["s09"].status = DraftStatus.REJECTED
    state.drafts["s10"].content = "Edited conclusion written by the reviewer."
    approved = WorkflowService.approve_all(state)
    assert approved >= 10 and state.drafts["s09"].status == DraftStatus.REJECTED

    record = c.workflow.generate_document(
        state, c.document_context(), 1, c.llm.describe(), ai_validation=True
    )
    assert (
        state.stage == WorkflowStage.DOCUMENT_GENERATED
        and state.final_validation is not None
    )
    out = docx.Document(record.docx_path)
    texts = [p.text for p in out.paragraphs]
    assert "Unified Customer Data Platform" in texts  # Title placeholder
    assert "Project proposal for Contoso Retail Group" in texts
    assert any(
        t.startswith(
            "Northwind Analytics Ltd. proposes a Unified Customer Data Platform for"
            " Contoso Retail Group, with"
        )
        for t in texts
    )
    assert "Edited conclusion written by the reviewer." in texts
    assert any(
        t.startswith("Identify the main risks") for t in texts
    )  # rejected section left untouched
    assert out.tables[0].cell(2, 1).text == "NWA-2026-014"
    assert (
        "Northwind Analytics Ltd. — Confidential"
        in out.sections[0].header.paragraphs[0].text
    )
    assert record.write_report.placeholders_remaining == []
    statuses = {chk.check: chk.status for chk in record.write_report.format_checks}
    assert "changed" not in statuses.values()

    json_text = Path(record.json_path).read_text(encoding="utf-8")
    payload = json.loads(json_text)
    assert payload["generation"]["version"] == 1 and payload["extraction"]["facts"]
    report_text = Path(record.report_path).read_text(encoding="utf-8")
    assert "Formatting preservation" in report_text
    assert record.comparison.modified > 0
    assert "sk-test" not in json_text


def test_populate_placeholders_task_and_versions(ready):
    c, _ = ready
    state = WorkflowService.new_state(TaskType.POPULATE_PLACEHOLDERS, "")
    c.workflow.run_pipeline(state, c.document_context())
    assert all(d.target_type == "placeholder" for d in state.drafts.values())
    WorkflowService.approve_all(state)
    r1 = c.workflow.generate_document(
        state, c.document_context(), 1, "x", ai_validation=False
    )
    r2 = c.workflow.generate_document(
        state, c.document_context(), 2, "x", ai_validation=False
    )
    assert r1.docx_path != r2.docx_path and r1.sha256 == r2.sha256


def test_fill_selected_and_regenerate(ready):
    c, model = ready
    state = WorkflowService.new_state(
        TaskType.FILL_SELECTED, "", selected_targets=["s04", "budget"]
    )
    c.workflow.run_pipeline(state, c.document_context())
    assert set(state.drafts) == {"s04", "budget"}
    before = len(model.invocations)
    c.workflow.regenerate(state, c.document_context(), "s04", "Make it shorter")
    assert len(model.invocations) > before and state.drafts["s04"].history
    prompt = model.invocations[-1]["messages"][0]["content"][0]["text"]
    assert "Make it shorter" in prompt and "s04" in prompt


def test_extract_structured_and_compare_tasks(ready):
    c, _ = ready
    state = WorkflowService.new_state(TaskType.EXTRACT_STRUCTURED, "")
    c.workflow.run_pipeline(state, c.document_context())
    assert (
        state.stage == WorkflowStage.COMPLETED
        and state.extraction.facts
        and not state.drafts
    )
    cmp_state = WorkflowService.new_state(TaskType.COMPARE_DOCUMENTS, "")
    c.workflow.run_pipeline(cmp_state, c.document_context())
    assert "Template vs. sample_company_profile.docx" in cmp_state.comparison_text


def test_validate_completeness_task(ready):
    c, _ = ready
    state = WorkflowService.new_state(TaskType.VALIDATE_COMPLETENESS, "")
    c.workflow.run_pipeline(state, c.document_context())
    assert state.validation is not None and state.stage == WorkflowStage.COMPLETED


def test_generation_requires_approval(ready):
    c, _ = ready
    state = WorkflowService.new_state(TaskType.POPULATE_PLACEHOLDERS, "")
    c.workflow.run_pipeline(state, c.document_context())
    with pytest.raises(WorkflowError, match="Approve"):
        c.workflow.generate_document(state, c.document_context(), 1, "x")


def test_missing_inputs_fail_gracefully(container):
    use_model(container, ScriptedModel(so.OUTPUTS))
    state = WorkflowService.new_state(TaskType.GENERATE_COMPLETE, "")
    with pytest.raises(WorkflowError, match="template"):
        container.workflow.run_pipeline(state, container.document_context())
    load_template(container)
    state = WorkflowService.new_state(TaskType.GENERATE_COMPLETE, "")
    with pytest.raises(WorkflowError, match="source document"):
        container.workflow.run_pipeline(state, container.document_context())
    assert state.template_analysis is not None  # earlier step results are kept
