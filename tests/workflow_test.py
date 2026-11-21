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


def _rewrite_docx_part(path: Path, part_name: str, mutate) -> Path:
    import zipfile

    out = path.with_name(path.stem + "_mutated" + path.suffix)
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == part_name:
                data = mutate(data)
            zout.writestr(item, data)
    return out


def _doc_with_text(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    document = docx.Document()
    document.add_paragraph(text)
    document.core_properties.title = "Original title"
    document.core_properties.creator = "Original creator"
    document.save(path)
    return path


def test_stable_docx_sha256_ignores_core_metadata_volatility(tmp_path):
    path = _doc_with_text(tmp_path, "base.docx", "Stable content")
    baseline = WorkflowService._stable_docx_sha256(path)

    mutated = _rewrite_docx_part(
        path,
        "docProps/core.xml",
        lambda data: (
            data.replace(
                b"<dc:title>Original title</dc:title>",
                b"<dc:title>Changed title</dc:title>",
            )
            .replace(
                b"<dc:creator>Original creator</dc:creator>",
                b"<dc:creator>Changed creator</dc:creator>",
            )
            .replace(b"<cp:revision>1</cp:revision>", b"<cp:revision>99</cp:revision>")
            .replace(
                b'xmlns:dcterms="http://purl.org/dc/terms/"',
                b'xmlns:dcterms="http://purl.org/dc/terms/"',
                1,
            )
        ),
    )
    # Inject explicit volatile timestamps/last-modified-by changes.
    mutated = _rewrite_docx_part(
        mutated,
        "docProps/core.xml",
        lambda data: (
            data.replace(
                b"<dcterms:created",
                b'<dcterms:created xsi:type="dcterms:W3CDTF">2001-01-01T00:00:00Z</dcterms:created><dcterms:created_old',
            )
            .replace(
                b"<dcterms:modified",
                b'<dcterms:modified xsi:type="dcterms:W3CDTF">2099-01-01T00:00:00Z</dcterms:modified><dcterms:modified_old',
            )
            .replace(
                b"</cp:coreProperties>",
                b"<cp:lastModifiedBy>someone-else</cp:lastModifiedBy></cp:coreProperties>",
            )
        ),
    )

    assert WorkflowService._stable_docx_sha256(path) == baseline
    assert WorkflowService._stable_docx_sha256(mutated) == baseline
    assert path.read_bytes() != mutated.read_bytes()


def test_stable_docx_sha256_detects_document_body_changes(tmp_path):
    a = _doc_with_text(tmp_path, "a.docx", "Alpha")
    b = _doc_with_text(tmp_path, "b.docx", "Beta")

    assert WorkflowService._stable_docx_sha256(
        a
    ) != WorkflowService._stable_docx_sha256(b)


def test_stable_docx_sha256_detects_non_core_package_changes(tmp_path):
    path = _doc_with_text(tmp_path, "base.docx", "Same body")
    baseline = WorkflowService._stable_docx_sha256(path)

    mutated = _rewrite_docx_part(
        path,
        "word/styles.xml",
        lambda data: data + b"\n<!-- stable-hash test marker -->",
    )

    assert WorkflowService._stable_docx_sha256(mutated) != baseline


def test_stable_docx_sha256_is_deterministic_across_resaves(tmp_path):
    path = _doc_with_text(tmp_path, "base.docx", "Deterministic content")
    baseline = WorkflowService._stable_docx_sha256(path)
    document = docx.Document(str(path))
    document.save(path)
    document = docx.Document(str(path))
    document.save(path)

    assert WorkflowService._stable_docx_sha256(path) == baseline


def test_stable_docx_sha256_identical_generated_files_match_despite_raw_bytes(tmp_path):
    from document_engine.document_writer import write_document
    from models.agent_models import SectionDraft

    template = _doc_with_text(tmp_path, "template.docx", "Hello {{name}}")
    draft = SectionDraft(
        target="name", target_type="placeholder", title="name", content="World"
    )
    out1 = tmp_path / "out1.docx"
    out2 = tmp_path / "out2.docx"
    write_document(template, out1, [draft])
    write_document(template, out2, [draft])

    assert WorkflowService._stable_docx_sha256(
        out1
    ) == WorkflowService._stable_docx_sha256(out2)
    assert docx.Document(str(out1)).paragraphs[0].text == "Hello World"
    assert docx.Document(str(out2)).paragraphs[0].text == "Hello World"
