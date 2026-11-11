"""Workflow orchestration for the document automation pipeline.

Pipeline for writing tasks::

    analyze template → extract information → (optional) research → generate → validate → quality review
    → human review (edit / approve / reject / regenerate / answer missing info)
    → final validation → populate the original DOCX → comparison and reports → download

Each step is a separate method so the UI can run them individually or as a pipeline, show progress,
and recover from a failed step without losing earlier results.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from agents import (
    document_analyzer,
    document_generator,
    document_validator,
    information_extractor,
    quality_reviewer,
    research_agent,
)
from document_engine.document_comparator import compare_documents, unified_text_diff
from document_engine.document_writer import write_document
from document_engine.section_mapper import (
    default_targets,
    fillable_sections,
    resolve_target,
    target_label,
)
from models.agent_models import (
    AgentRole,
    DraftStatus,
    ExtractionResult,
    GeneratedSection,
    GenerationResult,
    ResearchResult,
    SectionDraft,
    TaskType,
    ValidationIssue,
    ValidationReport,
    WorkflowStage,
    WorkflowState,
)
from models.document_models import GenerationRecord, SourceRef, TemplateStructure
from services.agent_manager import AgentManager, AgentRunError
from services.security import sanitize_error
from tools.context import DocumentContext
from tools.validation_tools import deterministic_checks

logger = logging.getLogger(__name__)
Progress = Callable[[str, str], None]  # (step, message)


class WorkflowError(RuntimeError):
    pass


def _noop(step: str, message: str) -> None:
    return None


class WorkflowService:
    def __init__(
        self,
        agents: AgentManager,
        generated_dir: Path,
        budget_provider: Callable[[], int] = lambda: 60_000,
    ) -> None:
        self.agents = agents
        self.generated_dir = generated_dir
        self._budget = budget_provider

    # ------------------------------------------------------------------ state
    @staticmethod
    def new_state(task: TaskType, instructions: str, **kwargs) -> WorkflowState:
        return WorkflowState(
            run_id=datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
            task_type=task,
            instructions=instructions,
            **kwargs,
        )

    def _run(
        self,
        state: WorkflowState,
        role: AgentRole,
        prompt: str,
        ctx: DocumentContext,
        step: str,
        progress: Progress,
    ):
        progress(step, f"Running {role.value.replace('_', ' ')}…")
        try:
            output, record = self.agents.run(
                role, prompt, ctx, set(state.approved_tools)
            )
        except AgentRunError as exc:
            state.agent_runs.append(exc.record)
            state.last_error = str(exc)
            state.log(step, str(exc), "error", role, exc.record.duration_ms)
            raise WorkflowError(str(exc)) from exc
        state.agent_runs.append(record)
        state.last_error = None
        tools = (
            f" using tools: {', '.join(record.tool_calls)}" if record.tool_calls else ""
        )
        state.log(
            step,
            f"Completed in {record.duration_ms / 1000:.1f}s{tools}.",
            "success",
            role,
            record.duration_ms,
        )
        return output

    @staticmethod
    def _require_template(ctx: DocumentContext) -> tuple[TemplateStructure, Path]:
        if ctx.template is None or ctx.template_path is None:
            raise WorkflowError("Upload a DOCX template first.")
        return ctx.template, ctx.template_path

    # ------------------------------------------------------------------ steps
    def analyze_template(
        self, state: WorkflowState, ctx: DocumentContext, progress: Progress = _noop
    ) -> None:
        self._require_template(ctx)
        prompt = document_analyzer.build_prompt(
            ctx, state.task_type, state.instructions
        )
        state.template_analysis = self._run(
            state,
            AgentRole.DOCUMENT_ANALYZER,
            prompt,
            ctx,
            "Analyze template",
            progress,
        )
        state.stage = WorkflowStage.TEMPLATE_ANALYZED

    def extract_information(
        self, state: WorkflowState, ctx: DocumentContext, progress: Progress = _noop
    ) -> None:
        if not ctx.sources:
            raise WorkflowError("Upload at least one source document first.")
        prompt = information_extractor.build_prompt(
            ctx,
            state.template_analysis,
            state.task_type,
            state.instructions,
            state.user_answers,
            self._budget(),
        )
        result: ExtractionResult = self._run(
            state,
            AgentRole.INFORMATION_EXTRACTOR,
            prompt,
            ctx,
            "Extract information",
            progress,
        )
        self._resolve_fact_sources(result, ctx)
        state.extraction = result
        state.stage = WorkflowStage.SOURCES_EXTRACTED
        if result.missing:
            critical = sum(1 for m in result.missing if m.critical)
            state.log(
                "Extract information",
                f"{len(result.missing)} information gap(s) found ({critical}"
                " critical).",
                "warning",
            )
        if result.conflicts:
            state.log(
                "Extract information",
                f"{len(result.conflicts)} conflict(s) between sources.",
                "warning",
            )

    @staticmethod
    def _resolve_fact_sources(result: ExtractionResult, ctx: DocumentContext) -> None:
        for fact in result.facts:
            refs: list[SourceRef] = []
            valid_ids = []
            for cid in fact.source_chunk_ids:
                chunk = ctx.chunk(cid.strip())
                if chunk is not None:
                    valid_ids.append(chunk.chunk_id)
                    ref = chunk.ref.model_copy()
                    excerpt = _excerpt(chunk.text, fact.value or fact.statement)
                    ref.excerpt = excerpt
                    refs.append(ref)
            fact.source_chunk_ids = valid_ids
            fact.sources = refs
            if not refs:
                fact.confidence = "low"

    def research(
        self, state: WorkflowState, ctx: DocumentContext, progress: Progress = _noop
    ) -> None:
        if not state.research_enabled:
            state.research = ResearchResult(
                performed=False,
                limitations=["Research was not requested for this run."],
            )
            state.stage = WorkflowStage.RESEARCHED
            return
        tool_names = [
            d.name for d in self.agents.agent_tools(AgentRole.RESEARCH_AGENT, ctx)
        ]
        if not any(
            name
            for name in tool_names
            if name
            not in {
                "search_source_documents",
                "get_source_chunk",
                "list_source_documents",
            }
        ):
            state.log(
                "Research",
                "No external research tools are authorized for the Research Agent;"
                " continuing with sources only.",
                "warning",
                AgentRole.RESEARCH_AGENT,
            )
        prompt = research_agent.build_prompt(
            state.task_type,
            state.instructions,
            state.extraction,
            state.research_topics,
            tool_names,
        )
        state.research = self._run(
            state, AgentRole.RESEARCH_AGENT, prompt, ctx, "Research", progress
        )
        state.stage = WorkflowStage.RESEARCHED

    # .................................................................. generation
    def resolve_targets(self, state: WorkflowState, ctx: DocumentContext) -> list[str]:
        """Targets for the generator, formatted as section ids and placeholder keys."""
        structure = ctx.template
        if structure is None:
            raise WorkflowError("Upload a DOCX template first.")
        task = state.task_type
        placeholder_keys = structure.placeholder_keys()
        sections = [s.section_id for s in fillable_sections(structure)]
        if state.selected_targets and task in (
            TaskType.FILL_SELECTED,
            TaskType.REWRITE,
            TaskType.IMPROVE_QUALITY,
            TaskType.SUMMARIZE_SOURCES,
        ):
            targets = [t.strip("{}") for t in state.selected_targets]
            return [t for t in targets if t in placeholder_keys or t in sections]
        if task == TaskType.POPULATE_PLACEHOLDERS:
            return placeholder_keys
        if task in (TaskType.REWRITE, TaskType.IMPROVE_QUALITY):
            return [
                s.section_id for s in fillable_sections(structure) if s.has_real_content
            ]
        if task == TaskType.SUMMARIZE_SOURCES:
            return []
        targets = [t.strip("{}") for t in default_targets(structure)]
        if state.template_analysis is not None:
            # The Document Analyzer decides which sections need content (it sees existing text and instructions).
            decisions = {
                s.section_id: s.should_fill for s in state.template_analysis.sections
            }
            targets = placeholder_keys + [
                sid for sid in sections if decisions.get(sid, sid in targets)
            ]
        return targets

    def generate(
        self,
        state: WorkflowState,
        ctx: DocumentContext,
        targets: list[str] | None = None,
        progress: Progress = _noop,
        extra_instruction: str = "",
    ) -> list[str]:
        template, _ = self._require_template(ctx)
        targets = targets if targets is not None else self.resolve_targets(state, ctx)
        if not targets:
            raise WorkflowError(
                "There is nothing to generate: no target sections or placeholders were"
                " selected or found."
            )
        existing = None
        if state.task_type in (TaskType.REWRITE, TaskType.IMPROVE_QUALITY):
            existing = {
                t: s.content_text
                for t in targets
                if (s := template.section(t)) is not None and s.content_text
            }
        instructions = state.instructions + (
            f"\n\nAdditional instruction for this generation: {extra_instruction}"
            if extra_instruction
            else ""
        )
        produced: list[str] = []
        # Generate in batches to keep each response manageable.
        for batch in _batches(targets, 6):
            prompt = document_generator.build_prompt(
                ctx,
                state.task_type,
                instructions,
                batch,
                state.template_analysis,
                state.extraction,
                state.research,
                state.user_answers,
                self._budget(),
                existing,
            )
            result: GenerationResult = self._run(
                state,
                AgentRole.DOCUMENT_GENERATOR,
                prompt,
                ctx,
                f"Generate ({', '.join(batch)})",
                progress,
            )
            produced += self._apply_generation(state, ctx, result, allowed=set(batch))
        if state.generation is None:
            state.generation = GenerationResult()
        state.stage = WorkflowStage.GENERATED
        return produced

    def _apply_generation(
        self,
        state: WorkflowState,
        ctx: DocumentContext,
        result: GenerationResult,
        allowed: set[str],
    ) -> list[str]:
        structure = ctx.template
        if structure is None:
            raise WorkflowError("Upload a DOCX template first.")
        facts = {
            f.fact_id: f for f in (state.extraction.facts if state.extraction else [])
        }
        applied = []
        kept: list[GeneratedSection] = (
            list(state.generation.sections) if state.generation else []
        )
        for gen in result.sections:
            resolved = resolve_target(structure, gen.target, gen.target_type)
            if resolved is None or resolved[1] not in allowed:
                state.log(
                    "Generate",
                    "Ignored output for unknown or unrequested target"
                    f" '{gen.target[:40]}'.",
                    "warning",
                )
                continue
            target_type, target = resolved
            gen.target, gen.target_type = target, target_type
            sources: list[SourceRef] = []
            for fid in gen.fact_ids:
                fact = facts.get(fid)
                if fact:
                    sources.extend(
                        s
                        for s in fact.sources
                        if all(s.chunk_id != x.chunk_id for x in sources)
                    )
            original = ""
            title = gen.title
            if target_type == "section":
                section = structure.section(target)
                original = section.content_text if section else ""
                title = section.title if section else gen.title
            previous = state.drafts.get(target)
            warnings = []
            if not gen.content.strip():
                warnings.append("The agent could not generate content for this target.")
            if gen.fact_ids and not sources:
                warnings.append("The cited facts have no valid source references.")
            draft = SectionDraft(
                target=target,
                target_type=target_type,  # type: ignore[arg-type]
                title=title or target_label(structure, target, target_type),
                content=gen.content.strip(),
                original_content=original,
                sources=sources[:20],
                fact_ids=gen.fact_ids,
                warnings=warnings,
                unsupported_claims=gen.unsupported_claims,
                needs_user_input=gen.needs_user_input,
                insert_mode=(
                    previous.insert_mode
                    if previous
                    else SectionDraft.model_fields["insert_mode"].default
                ),
                history=(
                    (previous.history + [previous.content])
                    if previous and previous.content
                    else []
                ),
            )
            state.drafts[target] = draft
            kept = [k for k in kept if k.target != target] + [gen]
            applied.append(target)
        state.generation = GenerationResult(sections=kept)
        missing_targets = sorted(allowed - set(applied))
        if missing_targets:
            state.log(
                "Generate",
                f"No content returned for: {', '.join(missing_targets)}.",
                "warning",
            )
        return applied

    def regenerate(
        self,
        state: WorkflowState,
        ctx: DocumentContext,
        target: str,
        instruction: str = "",
        progress: Progress = _noop,
    ) -> None:
        self.generate(state, ctx, [target], progress, extra_instruction=instruction)
        draft = state.drafts.get(target)
        if draft:
            draft.status = DraftStatus.PENDING
            draft.edited = False

    # .................................................................. validation & review
    def validate(
        self,
        state: WorkflowState,
        ctx: DocumentContext,
        progress: Progress = _noop,
        drafts: list[SectionDraft] | None = None,
        final: bool = False,
        use_agent: bool = True,
    ) -> ValidationReport:
        drafts = drafts if drafts is not None else list(state.drafts.values())
        pre = deterministic_checks(drafts, ctx, state.user_answers)
        if final and ctx.template is not None:
            covered = {d.target for d in drafts}
            for key in ctx.template.placeholder_keys():
                if key not in covered and key not in state.user_answers:
                    pre.append(
                        ValidationIssue(
                            severity="warning",
                            target=key,
                            category="placeholder",
                            message=(
                                f"Placeholder {{{{{key}}}}} has no approved value and"
                                " will remain in the document."
                            ),
                        )
                    )
        report: ValidationReport
        if use_agent and drafts:
            prompt = document_validator.build_prompt(
                ctx,
                state.task_type,
                state.instructions,
                drafts,
                state.extraction,
                pre,
                state.user_answers,
            )
            report = self._run(
                state,
                AgentRole.DOCUMENT_VALIDATOR,
                prompt,
                ctx,
                "Final validation" if final else "Validate",
                progress,
            )
            existing = {(i.target, i.message) for i in report.issues}
            report.issues = [
                i for i in pre if (i.target, i.message) not in existing
            ] + report.issues
        else:
            errors = sum(1 for i in pre if i.severity == "error")
            score = max(
                0.0,
                100.0
                - 15 * errors
                - 5 * sum(1 for i in pre if i.severity == "warning"),
            )
            report = ValidationReport(
                complete=errors == 0,
                completeness_score=score,
                issues=pre,
                summary="Automated checks only.",
            )
        for draft in drafts:
            draft.warnings = [
                w for w in draft.warnings if not w.startswith("Validation:")
            ]
            for issue in report.issues:
                if (
                    issue.target
                    and draft.target in [t.strip() for t in issue.target.split(",")]
                    and issue.severity in ("error", "warning")
                ):
                    draft.warnings.append(f"Validation: {issue.message}")
        if final:
            state.final_validation = report
        else:
            state.validation = report
            state.stage = WorkflowStage.VALIDATED
        return report

    def quality_review(
        self, state: WorkflowState, ctx: DocumentContext, progress: Progress = _noop
    ) -> None:
        drafts = [d for d in state.drafts.values() if d.content.strip()]
        if not drafts:
            return
        prompt = quality_reviewer.build_prompt(
            state.task_type, state.instructions, drafts, state.template_analysis
        )
        state.quality = self._run(
            state, AgentRole.QUALITY_REVIEWER, prompt, ctx, "Quality review", progress
        )
        for revision in state.quality.revisions:
            draft = state.drafts.get(
                revision.target.strip("{} ").lower()
            ) or state.drafts.get(revision.target.strip("{} "))
            if (
                draft
                and revision.revised_content.strip()
                and revision.revised_content.strip() != draft.content
            ):
                draft.revision_suggestion = revision.revised_content.strip()
        state.stage = WorkflowStage.REVIEWED

    @staticmethod
    def apply_revision(state: WorkflowState, target: str) -> None:
        draft = state.drafts[target]
        if draft.revision_suggestion:
            draft.history.append(draft.content)
            draft.content = draft.revision_suggestion
            draft.revision_suggestion = None
            draft.edited = True
            draft.status = DraftStatus.PENDING

    # ------------------------------------------------------------------ pipeline
    def run_pipeline(
        self, state: WorkflowState, ctx: DocumentContext, progress: Progress = _noop
    ) -> WorkflowState:
        """Run all automatic steps for the selected task, stopping before human approval."""
        task = state.task_type
        state.drafts = {} if state.stage == WorkflowStage.NOT_STARTED else state.drafts
        started = time.monotonic()
        try:
            if task == TaskType.COMPARE_DOCUMENTS:
                self.compare_with_sources(state, ctx, progress)
                state.stage = WorkflowStage.COMPLETED
                return state
            self.analyze_template(state, ctx, progress)
            if task == TaskType.IMPROVE_QUALITY:
                self._drafts_from_existing(state, ctx)
                self.quality_review(state, ctx, progress)
                for d in state.drafts.values():
                    if d.revision_suggestion:
                        WorkflowService.apply_revision(state, d.target)
                return state
            if task == TaskType.VALIDATE_COMPLETENESS:
                if ctx.sources:
                    self.extract_information(state, ctx, progress)
                self._drafts_from_existing(state, ctx, include_placeholders=True)
                self.validate(state, ctx, progress)
                state.stage = WorkflowStage.COMPLETED
                return state
            self.extract_information(state, ctx, progress)
            if task == TaskType.EXTRACT_STRUCTURED:
                state.stage = WorkflowStage.COMPLETED
                return state
            self.research(state, ctx, progress)
            if task == TaskType.SUMMARIZE_SOURCES:
                self.summarize(state, ctx, progress)
                if not state.selected_targets:
                    state.stage = WorkflowStage.COMPLETED
                    return state
            else:
                self.generate(state, ctx, progress=progress)
            self.validate(state, ctx, progress)
            self.quality_review(state, ctx, progress)
        except WorkflowError:
            state.stage = (
                WorkflowStage.FAILED
                if state.stage == WorkflowStage.NOT_STARTED
                else state.stage
            )
            raise
        finally:
            state.log(
                "Pipeline",
                f"Automatic steps finished after {time.monotonic() - started:.1f}s.",
            )
        return state

    def summarize(
        self, state: WorkflowState, ctx: DocumentContext, progress: Progress = _noop
    ) -> None:
        targets = self.resolve_targets(state, ctx) if state.selected_targets else []
        if targets:
            self.generate(
                state,
                ctx,
                targets,
                progress,
                extra_instruction=(
                    "Write a summary of the source documents for these targets."
                ),
            )
            state.summary_text = "\n\n".join(
                state.drafts[t].content for t in targets if t in state.drafts
            )
            return
        # Free-standing summary: use the generator on the first fillable section as a scratch target.
        if ctx.template is None:
            raise WorkflowError("Upload a DOCX template first.")
        sections = fillable_sections(ctx.template)
        if not sections:
            raise WorkflowError(
                "The template has no sections; select a target for the summary."
            )
        scratch = sections[0].section_id
        self.generate(
            state,
            ctx,
            [scratch],
            progress,
            extra_instruction=(
                "Ignore the section's normal purpose: write a structured summary of ALL"
                " source documents (key facts, figures, dates, parties, risks)."
            ),
        )
        draft = state.drafts.pop(scratch, None)
        state.summary_text = draft.content if draft else ""

    def _drafts_from_existing(
        self,
        state: WorkflowState,
        ctx: DocumentContext,
        include_placeholders: bool = False,
    ) -> None:
        if ctx.template is None:
            raise WorkflowError("Upload a DOCX template first.")
        targets = (
            set(self.resolve_targets(state, ctx))
            if state.task_type == TaskType.IMPROVE_QUALITY
            else None
        )
        for section in fillable_sections(ctx.template):
            if targets is not None and section.section_id not in targets:
                continue
            content = "\n\n".join(
                line for line in section.content_text.split("\n") if line.strip()
            )
            state.drafts[section.section_id] = SectionDraft(
                target=section.section_id,
                target_type="section",
                title=section.title,
                content=content if section.has_real_content else "",
                original_content=section.content_text,
            )
        if include_placeholders:
            for ph in ctx.template.placeholders:
                state.drafts[ph.key] = SectionDraft(
                    target=ph.key,
                    target_type="placeholder",
                    title=f"{{{{{ph.key}}}}}",
                    content="",
                )

    def compare_with_sources(
        self, state: WorkflowState, ctx: DocumentContext, progress: Progress = _noop
    ) -> None:
        _, template_path = self._require_template(ctx)
        docx_sources = [d for d in ctx.sources.values() if d.file_type == "docx"]
        if state.selected_targets:
            docx_sources = [
                d for d in docx_sources if d.doc_id in state.selected_targets
            ] or docx_sources
        if not docx_sources:
            raise WorkflowError(
                "Upload a DOCX source document to compare with the template."
            )
        lines = []
        for doc in docx_sources:
            progress("Compare", f"Comparing the template with {doc.file_name}…")
            report = compare_documents(template_path, doc.stored_path)
            lines.append(
                f"## Template vs. {doc.file_name}\nBlocks: {report.original_blocks} →"
                f" {report.generated_blocks}; added {report.added}, removed"
                f" {report.removed}, modified {report.modified}, unchanged"
                f" {report.unchanged}.\n"
            )
            lines.append(
                "```diff\n"
                + unified_text_diff(template_path, doc.stored_path)[:20_000]
                + "\n```"
            )  # type: ignore[arg-type]
        state.comparison_text = "\n\n".join(lines)
        state.log(
            "Compare",
            f"Compared the template with {len(docx_sources)} document(s).",
            "success",
        )

    # ------------------------------------------------------------------ answers & approval
    @staticmethod
    def record_answer(
        state: WorkflowState, target: str, question: str, answer: str, structure
    ) -> None:
        answer = answer.strip()
        if not answer:
            return
        state.user_answers[f"{target}: {question}"[:200]] = answer
        if structure is not None and target in structure.placeholder_keys():
            existing = state.drafts.get(target)
            state.drafts[target] = SectionDraft(
                target=target,
                target_type="placeholder",
                title=f"{{{{{target}}}}}",
                content=answer,
                sources=[
                    SourceRef(
                        doc_id="user",
                        file_name="User answer",
                        location="Document Review",
                    )
                ],
                history=(
                    (existing.history + [existing.content])
                    if existing and existing.content
                    else []
                ),
                edited=True,
            )

    @staticmethod
    def approve_all(state: WorkflowState) -> int:
        count = 0
        for draft in state.drafts.values():
            if draft.status == DraftStatus.PENDING and draft.content.strip():
                draft.status = DraftStatus.APPROVED
                count += 1
        if state.approved_drafts:
            state.stage = WorkflowStage.APPROVED
        return count

    # ------------------------------------------------------------------ final document
    def generate_document(
        self,
        state: WorkflowState,
        ctx: DocumentContext,
        version: int,
        provider_label: str,
        ai_validation: bool = True,
        progress: Progress = _noop,
    ) -> GenerationRecord:
        template, template_path = self._require_template(ctx)
        approved = state.approved_drafts
        if not approved:
            raise WorkflowError(
                "Approve at least one section or placeholder before generating the"
                " document."
            )
        report = self.validate(
            state,
            ctx,
            progress,
            drafts=approved,
            final=True,
            use_agent=ai_validation and self.agents.llm.is_configured(),
        )
        out_dir = self.generated_dir / state.run_id
        stem = Path(template.file_name).stem[:60] or "document"
        file_name = f"{stem}_v{version}.docx"
        out_path = out_dir / file_name
        progress("Generate document", "Populating the original template…")
        try:
            write_report = write_document(
                template_path,
                out_path,
                approved,
            )
        except Exception as exc:
            state.log(
                "Generate document",
                f"Writing the document failed: {sanitize_error(exc)}",
                "error",
            )
            raise WorkflowError(
                f"Writing the document failed: {sanitize_error(exc)}"
            ) from exc
        comparison = compare_documents(template_path, out_path)
        sha = __import__("hashlib").sha256(out_path.read_bytes()).hexdigest()
        record = GenerationRecord(
            version=version,
            run_id=state.run_id,
            template_name=template.file_name,
            file_name=file_name,
            docx_path=str(out_path),
            sha256=sha,
            sections_applied=[d.target for d in approved if d.target_type == "section"],
            placeholders_applied=[
                d.target for d in approved if d.target_type == "placeholder"
            ],
            provider_label=provider_label,
            validation_summary=(
                f"{report.completeness_score:.0f}% complete ·"
                f" {sum(1 for i in report.issues if i.severity == 'error')} error(s),"
                f" {sum(1 for i in report.issues if i.severity == 'warning')}"
                " warning(s)"
            ),
            write_report=write_report,
            comparison=comparison,
        )
        json_path = out_dir / f"{stem}_v{version}.json"
        json_path.write_text(self.export_json(state, record), encoding="utf-8")
        report_path = out_dir / f"{stem}_v{version}_report.md"
        report_path.write_text(self.completion_report(state, record), encoding="utf-8")
        record.json_path, record.report_path = str(json_path), str(report_path)
        state.stage = WorkflowStage.DOCUMENT_GENERATED
        state.log(
            "Generate document",
            f"Created {file_name} with {len(approved)} approved item(s).",
            "success",
        )
        return record

    @staticmethod
    def export_json(
        state: WorkflowState, record: GenerationRecord | None = None
    ) -> str:
        payload = {
            "run_id": state.run_id,
            "task": state.task_type.value,
            "instructions": state.instructions,
            "drafts": [d.model_dump(mode="json") for d in state.drafts.values()],
            "template_analysis": (
                state.template_analysis.model_dump(mode="json")
                if state.template_analysis
                else None
            ),
            "extraction": (
                state.extraction.model_dump(mode="json") if state.extraction else None
            ),
            "research": (
                state.research.model_dump(mode="json") if state.research else None
            ),
            "validation": (
                state.validation.model_dump(mode="json") if state.validation else None
            ),
            "final_validation": (
                state.final_validation.model_dump(mode="json")
                if state.final_validation
                else None
            ),
            "quality": state.quality.model_dump(mode="json") if state.quality else None,
            "summary": state.summary_text,
            "user_answers": state.user_answers,
            "generation": (
                record.model_dump(
                    mode="json", exclude={"docx_path", "json_path", "report_path"}
                )
                if record
                else None
            ),
        }
        return json.dumps(payload, indent=2, default=str)

    @staticmethod
    def completion_report(state: WorkflowState, record: GenerationRecord) -> str:
        wr = record.write_report
        lines = [
            f"# Completion report — {record.file_name}",
            f"- Run: {state.run_id}",
            f"- Template: {record.template_name}",
            f"- Model: {record.provider_label}",
            f"- Created: {record.created_at:%Y-%m-%d %H:%M}",
            f"- Validation: {record.validation_summary}",
            "",
            "## Applied content",
            *(
                f"- Section {s}: {state.drafts[s].title}"
                for s in record.sections_applied
                if s in state.drafts
            ),
            *(
                f"- Placeholder {{{{{k}}}}} replaced"
                f" {wr.placeholders_replaced.get(k, 0)}×"
                for k in record.placeholders_applied
            ),
            "",
            "## Placeholders remaining",
            *(f"- {{{{{k}}}}}" for k in wr.placeholders_remaining),
            "" if wr.placeholders_remaining else "- None",
            "## Formatting preservation",
            *(
                f"- {c.status.upper()}:"
                f" {c.check}{(' — ' + c.detail) if c.detail else ''}"
                for c in wr.format_checks
            ),
            "",
            "## Validation issues",
        ]
        report = state.final_validation
        if report and report.issues:
            lines += [
                f"- [{i.severity}] {i.target or ''}: {i.message}" for i in report.issues
            ]
        else:
            lines.append("- None")
        if record.comparison:
            c = record.comparison
            lines += [
                "",
                "## Comparison with the original",
                (
                    f"- Added {c.added}, removed {c.removed}, modified {c.modified},"
                    f" unchanged {c.unchanged} block(s)."
                ),
            ]
        return "\n".join(lines) + "\n"


def _batches(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _excerpt(text: str, needle: str, width: int = 160) -> str:
    if needle:
        idx = text.lower().find(needle.lower()[:40])
        if idx >= 0:
            start = max(0, idx - width // 3)
            return text[start : start + width].replace("\n", " ").strip()
    return text[:width].replace("\n", " ").strip()
