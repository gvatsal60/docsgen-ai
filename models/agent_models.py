"""Structured inputs/outputs exchanged with the Strands agents, and workflow state."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from models.document_models import SourceRef


class AgentRole(str, Enum):
    DOCUMENT_ANALYZER = "document_analyzer"
    INFORMATION_EXTRACTOR = "information_extractor"
    RESEARCH_AGENT = "research_agent"
    DOCUMENT_GENERATOR = "document_generator"
    DOCUMENT_VALIDATOR = "document_validator"
    QUALITY_REVIEWER = "quality_reviewer"


AGENT_LABELS = {
    AgentRole.DOCUMENT_ANALYZER: "Document Analyzer",
    AgentRole.INFORMATION_EXTRACTOR: "Information Extractor",
    AgentRole.RESEARCH_AGENT: "Research Agent",
    AgentRole.DOCUMENT_GENERATOR: "Document Generator",
    AgentRole.DOCUMENT_VALIDATOR: "Document Validator",
    AgentRole.QUALITY_REVIEWER: "Quality Reviewer",
}

TargetType = Literal["section", "placeholder"]


# ------------------------------------------------------------------ Agent 1: Document Analyzer
class SectionRequirement(BaseModel):
    """What a template section needs."""

    section_id: str = Field(
        description=(
            "Section identifier exactly as given in the template outline, e.g. s03."
        )
    )
    title: str
    purpose: str = Field(
        description="One sentence describing what this section should contain."
    )
    required_information: list[str] = Field(
        default_factory=list, description="Specific facts this section needs."
    )
    expected_length: Literal["short", "medium", "long"] = "medium"
    should_fill: bool = Field(
        default=True,
        description=(
            "False for sections that must stay unchanged (e.g. cover, table of"
            " contents)."
        ),
    )


class PlaceholderRequirement(BaseModel):
    key: str = Field(description="Placeholder key without braces, exactly as listed.")
    description: str
    expected_type: Literal[
        "text", "date", "number", "currency", "name", "paragraph", "other"
    ] = "text"


class TemplateAnalysis(BaseModel):
    """Output of the Document Analyzer."""

    document_type: str = Field(
        description="What kind of document the template is, e.g. 'project proposal'."
    )
    summary: str
    sections: list[SectionRequirement] = Field(default_factory=list)
    placeholders: list[PlaceholderRequirement] = Field(default_factory=list)
    writing_style: str = Field(
        default="", description="Observed tone, person and terminology to follow."
    )
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------- Agent 2: Information Extraction
class Fact(BaseModel):
    fact_id: str = Field(description="Short unique id such as F1, F2.")
    statement: str = Field(description="The fact as a complete sentence.")
    value: str | None = Field(
        default=None, description="The key value (date, amount, name) if any."
    )
    category: Literal[
        "date",
        "number",
        "money",
        "name",
        "organization",
        "reference",
        "requirement",
        "other",
    ] = "other"
    source_chunk_ids: list[str] = Field(
        default_factory=list,
        description="Chunk ids (e.g. D1-C3) the fact was taken from.",
    )
    confidence: Literal["high", "medium", "low"] = "medium"
    sources: list[SourceRef] = Field(
        default_factory=list, description="Filled in by the application; leave empty."
    )


class SectionMapping(BaseModel):
    target: str = Field(
        description="Section id (e.g. s04) or placeholder key (e.g. company_name)."
    )
    target_type: Literal["section", "placeholder"]
    fact_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class Conflict(BaseModel):
    description: str
    fact_ids: list[str] = Field(default_factory=list)


class MissingInfo(BaseModel):
    target: str = Field(
        description="Section id or placeholder key that lacks information."
    )
    description: str
    critical: bool = False
    question_for_user: str = Field(
        default="",
        description="A precise question the user can answer to fill the gap.",
    )


class ExtractionResult(BaseModel):
    """Output of the Information Extractor."""

    facts: list[Fact] = Field(default_factory=list)
    mappings: list[SectionMapping] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    missing: list[MissingInfo] = Field(default_factory=list)


# ------------------------------------------------------------------------ Agent 3: Research
class ResearchFinding(BaseModel):
    statement: str
    verified: bool = Field(
        description="True only if confirmed by a cited source returned by a tool."
    )
    citation_title: str | None = None
    citation_url: str | None = None
    tool_used: str | None = None
    related_target: str | None = None


class ResearchResult(BaseModel):
    performed: bool
    findings: list[ResearchFinding] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


# -------------------------------------------------------------------- Agent 4: Generation
class GeneratedSection(BaseModel):
    target: str = Field(description="Section id (e.g. s04) or placeholder key.")
    target_type: Literal["section", "placeholder"]
    title: str = ""
    content: str = Field(
        description=(
            "Content to insert. For sections use plain paragraphs separated by blank"
            " lines; bullet items start with '- ', numbered items with '1. '; **bold**"
            " is allowed. For placeholders give only the value."
        )
    )
    fact_ids: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(
        default_factory=list,
        description="Statements you could not support from sources.",
    )
    needs_user_input: list[str] = Field(
        default_factory=list, description="Information still required from the user."
    )


class GenerationResult(BaseModel):
    sections: list[GeneratedSection] = Field(default_factory=list)


# --------------------------------------------------------------------- Agent 5: Validation
class ValidationIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    target: str | None = None
    category: Literal[
        "missing",
        "inconsistency",
        "number",
        "date",
        "duplicate",
        "unsupported",
        "placeholder",
        "other",
    ] = "other"
    message: str
    suggestion: str = ""


class ValidationReport(BaseModel):
    complete: bool
    completeness_score: float = Field(ge=0, le=100)
    issues: list[ValidationIssue] = Field(default_factory=list)
    summary: str = ""


# ------------------------------------------------------------------ Agent 6: Quality review
class QualityRevision(BaseModel):
    target: str
    revised_content: str = Field(
        description="Full improved content for the target. Must not add new facts."
    )
    rationale: str


class QualityReport(BaseModel):
    overall_score: float = Field(ge=0, le=100)
    grammar: str = ""
    readability: str = ""
    consistency: str = ""
    formatting: str = ""
    revisions: list[QualityRevision] = Field(default_factory=list)
    summary: str = ""


# --------------------------------------------------------------------------- Workflow state
class TaskType(str, Enum):
    GENERATE_COMPLETE = "generate_complete"
    FILL_SELECTED = "fill_selected"
    POPULATE_PLACEHOLDERS = "populate_placeholders"
    REWRITE = "rewrite"
    SUMMARIZE_SOURCES = "summarize_sources"
    EXTRACT_STRUCTURED = "extract_structured"
    VALIDATE_COMPLETENESS = "validate_completeness"
    IMPROVE_QUALITY = "improve_quality"
    COMPARE_DOCUMENTS = "compare_documents"


TASK_LABELS = {
    TaskType.GENERATE_COMPLETE: "Generate complete document",
    TaskType.FILL_SELECTED: "Fill selected sections",
    TaskType.POPULATE_PLACEHOLDERS: "Populate placeholders",
    TaskType.REWRITE: "Rewrite content",
    TaskType.SUMMARIZE_SOURCES: "Summarize source documents",
    TaskType.EXTRACT_STRUCTURED: "Extract structured information",
    TaskType.VALIDATE_COMPLETENESS: "Validate document completeness",
    TaskType.IMPROVE_QUALITY: "Improve document quality",
    TaskType.COMPARE_DOCUMENTS: "Compare documents",
}

#: Tasks whose output is applied to the template.
WRITING_TASKS = {
    TaskType.GENERATE_COMPLETE,
    TaskType.FILL_SELECTED,
    TaskType.POPULATE_PLACEHOLDERS,
    TaskType.REWRITE,
    TaskType.IMPROVE_QUALITY,
    TaskType.SUMMARIZE_SOURCES,
}


class DraftStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class InsertMode(str, Enum):
    AUTO = "auto"  # replace empty/instructional content, otherwise append
    REPLACE = "replace"  # replace the whole section body
    APPEND = "append"  # keep existing content and append


class SectionDraft(BaseModel):
    target: str
    target_type: TargetType
    title: str
    content: str
    original_content: str = ""
    status: DraftStatus = DraftStatus.PENDING
    insert_mode: InsertMode = InsertMode.AUTO
    sources: list[SourceRef] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    needs_user_input: list[str] = Field(default_factory=list)
    revision_suggestion: str | None = None
    history: list[str] = Field(default_factory=list)
    edited: bool = False


class WorkflowStage(str, Enum):
    NOT_STARTED = "not_started"
    TEMPLATE_ANALYZED = "template_analyzed"
    SOURCES_EXTRACTED = "sources_extracted"
    RESEARCHED = "researched"
    GENERATED = "generated"
    VALIDATED = "validated"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    DOCUMENT_GENERATED = "document_generated"
    COMPLETED = "completed"  # for non-writing tasks
    FAILED = "failed"


STAGE_ORDER = [
    WorkflowStage.NOT_STARTED,
    WorkflowStage.TEMPLATE_ANALYZED,
    WorkflowStage.SOURCES_EXTRACTED,
    WorkflowStage.RESEARCHED,
    WorkflowStage.GENERATED,
    WorkflowStage.VALIDATED,
    WorkflowStage.REVIEWED,
    WorkflowStage.APPROVED,
    WorkflowStage.DOCUMENT_GENERATED,
]


class WorkflowEvent(BaseModel):
    at: datetime = Field(default_factory=datetime.now)
    step: str
    level: Literal["info", "success", "warning", "error"] = "info"
    message: str
    agent: AgentRole | None = None
    duration_ms: int | None = None


class AgentRunRecord(BaseModel):
    role: AgentRole
    ok: bool
    duration_ms: int
    tools_available: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    model_label: str = ""
    error: str | None = None


class WorkflowState(BaseModel):
    run_id: str
    task_type: TaskType = TaskType.GENERATE_COMPLETE
    instructions: str = ""
    selected_targets: list[str] = Field(default_factory=list)
    research_enabled: bool = False
    research_topics: str = ""
    approved_tools: list[str] = Field(default_factory=list)

    stage: WorkflowStage = WorkflowStage.NOT_STARTED
    template_analysis: TemplateAnalysis | None = None
    extraction: ExtractionResult | None = None
    research: ResearchResult | None = None
    generation: GenerationResult | None = None
    validation: ValidationReport | None = None
    quality: QualityReport | None = None
    final_validation: ValidationReport | None = None
    summary_text: str | None = None
    comparison_text: str | None = None

    drafts: dict[str, SectionDraft] = Field(default_factory=dict)
    user_answers: dict[str, str] = Field(default_factory=dict)
    events: list[WorkflowEvent] = Field(default_factory=list)
    agent_runs: list[AgentRunRecord] = Field(default_factory=list)
    last_error: str | None = None

    def log(
        self,
        step: str,
        message: str,
        level: str = "info",
        agent: AgentRole | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self.events.append(
            WorkflowEvent(
                step=step,
                message=message,
                level=level,
                agent=agent,
                duration_ms=duration_ms,
            )
        )  # type: ignore[arg-type]

    @property
    def approved_drafts(self) -> list[SectionDraft]:
        return [d for d in self.drafts.values() if d.status == DraftStatus.APPROVED]

    @property
    def pending_drafts(self) -> list[SectionDraft]:
        return [d for d in self.drafts.values() if d.status == DraftStatus.PENDING]
