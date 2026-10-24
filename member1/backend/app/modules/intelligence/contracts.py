"""Version 1.0 boundary models. Other modules consume these JSON contracts."""

from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdeaSpec(Contract):
    original_text: str = Field(min_length=10, max_length=12000)
    project_name: str = Field(min_length=1, max_length=200)
    problem: str = Field(min_length=1, max_length=3000)
    solution: str = Field(min_length=1, max_length=3000)
    target_users: list[str] = Field(default_factory=list, max_length=20)
    domain: str = Field(min_length=1, max_length=200)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    field_origins: dict[str, Literal["provided", "inferred", "unknown"]] = Field(default_factory=dict)


class Source(Contract):
    source_id: str
    title: str
    url: str
    retrieved_at: datetime = Field(default_factory=utc_now)
    publisher: str | None = None
    kind: Literal["web", "fixture"] = "web"


class ResearchDocument(Contract):
    source: Source
    content: str = Field(min_length=1)


class EvidenceItem(Contract):
    evidence_id: str
    source_id: str
    excerpt: str = Field(min_length=1)
    locator: str | None = None
    relevance_score: float = 0.0


class SourcedFigure(Contract):
    figure_id: str
    display_value: str
    metric: str
    excerpt: str
    evidence_id: str
    source_id: str
    qualification: str


class Claim(Contract):
    claim_id: str
    text: str = Field(min_length=1)
    kind: Literal["supported", "assumption", "unknown"]
    evidence_ids: list[str] = Field(default_factory=list)


class AnalysisSection(Contract):
    section_id: str
    heading: str
    claims: list[Claim] = Field(default_factory=list)


class AnalysisReport(Contract):
    summary: str
    sections: list[AnalysisSection]
    gaps: list[str] = Field(default_factory=list)


class CriticIssue(Contract):
    issue_id: str
    severity: Literal["high", "medium", "low"]
    claim_id: str | None = None
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    resolution: str | None = None


class CritiqueReport(Contract):
    review_status: Literal["ready", "ready_with_gaps", "blocked"]
    issues: list[CriticIssue] = Field(default_factory=list)
    revision_summary: str = ""
    unresolved_gaps: list[str] = Field(default_factory=list)


class ExecutionInfo(Contract):
    mode: Literal["demo", "live"]
    llm_provider: str
    search_provider: str
    retrieval_method: str
    revision_passes: int = Field(default=0, ge=0, le=1)
    model_id: str | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_hash(self, handler):
        payload = handler(self)
        if self.model_id is None:
            payload.pop("model_id", None)
        return payload


class ContextDraft(Contract):
    schema_version: Literal["1.0"] = "1.0"
    idea: IdeaSpec
    sources: list[Source]
    evidence: list[EvidenceItem]
    initial_analysis: AnalysisReport
    final_analysis: AnalysisReport
    critic: CritiqueReport
    warnings: list[str] = Field(default_factory=list)
    execution: ExecutionInfo
    figures: list[SourcedFigure] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_references(self) -> "ContextDraft":
        source_ids = [s.source_id for s in self.sources]
        evidence_ids = [e.evidence_id for e in self.evidence]
        if len(source_ids) != len(set(source_ids)) or len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Source and evidence IDs must be unique")
        for item in self.evidence:
            if item.source_id not in source_ids:
                raise ValueError("Evidence references an unknown source")
        evidence_by_id = {item.evidence_id: item for item in self.evidence}
        figure_ids = [figure.figure_id for figure in self.figures]
        if len(figure_ids) != len(set(figure_ids)):
            raise ValueError("Figure IDs must be unique")
        for figure in self.figures:
            if figure.evidence_id not in evidence_ids or figure.source_id not in source_ids:
                raise ValueError("Figure references unknown evidence or source")
            cited = evidence_by_id[figure.evidence_id]
            if figure.source_id != cited.source_id:
                raise ValueError("Figure source must match its cited evidence")
            if not figure.excerpt or figure.excerpt not in cited.excerpt or not figure.display_value or figure.display_value not in figure.excerpt:
                raise ValueError("Figure value and excerpt must be quoted from its evidence")
        claim_ids = [c.claim_id for s in self.final_analysis.sections for c in s.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("Final claim IDs must be unique")
        for section in self.final_analysis.sections:
            for claim in section.claims:
                if any(ref not in evidence_ids for ref in claim.evidence_ids):
                    raise ValueError("Final claim references unknown evidence")
                if claim.kind == "supported" and not claim.evidence_ids:
                    raise ValueError("Supported final claims require evidence")
        return self


class ProjectContext(ContextDraft):
    project_id: str
    context_version: int = Field(ge=1)
    created_at: datetime
    content_hash: str


class ResearchOptions(Contract):
    max_sources: int = Field(default=5, ge=1, le=8)
    language: str = Field(default="en", pattern=r"^[a-z]{2}$")
    region: str | None = Field(default=None, max_length=100)
    source_urls: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def public_source_urls(self) -> "ResearchOptions":
        for url in self.source_urls:
            try:
                parsed = urlsplit(url)
                valid = (len(url) <= 3000 and parsed.scheme in {"http", "https"}
                         and bool(parsed.hostname) and not parsed.username and not parsed.password
                         and parsed.port in {None, 80, 443})
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Source URLs must be HTTP(S) links without credentials, using standard web ports")
        return self


class ExtractionRequest(Contract):
    project_id: str = Field(min_length=1, max_length=100)
    idea_text: str = Field(min_length=10, max_length=12000)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)


class AnalysisRequest(Contract):
    project_id: str = Field(min_length=1, max_length=100)
    idea: IdeaSpec
    research_options: ResearchOptions = Field(default_factory=ResearchOptions)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)


class AnalysisResult(Contract):
    analysis_id: str
    project_id: str
    context_draft: ContextDraft
    stages: list[str]
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    figures: list[SourcedFigure] = Field(default_factory=list)
    model_id: str | None = None

    @model_validator(mode="after")
    def matching_figures(self) -> "AnalysisResult":
        if self.figures != self.context_draft.figures:
            raise ValueError("Analysis figures must match the context draft")
        if self.model_id and self.context_draft.execution.model_id and self.model_id != self.context_draft.execution.model_id:
            raise ValueError("Analysis model must match its execution metadata")
        return self


class DebateRequest(Contract):
    project_id: str = Field(min_length=1, max_length=100)
    analysis_id: str
    mode: Literal["user_vs_agent", "agent_vs_agent"]
    user_stance: Literal["for", "against"] = "for"
    max_rounds: int = Field(default=10, ge=1, le=100, strict=True)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)


class DebateTurnRequest(Contract):
    message: str = Field(min_length=1, max_length=6000)


class DebateTurn(Contract):
    turn_id: str
    round_number: int = Field(ge=0)
    speaker: Literal["user", "pro", "skeptic", "moderator"]
    stance: Literal["for", "against", "neutral"]
    message: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    figures: list[SourcedFigure] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class DebateSession(Contract):
    debate_id: str
    project_id: str
    analysis_id: str
    mode: Literal["user_vs_agent", "agent_vs_agent"]
    user_stance: Literal["for", "against"]
    max_rounds: int = Field(ge=1, le=100, strict=True)
    status: Literal["active", "completed", "failed"] = "active"
    turns: list[DebateTurn] = Field(default_factory=list)
    summary: str | None = None
    history_compacted: bool = False
    history_note: str | None = None
    model_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


AnalysisResult.model_rebuild()


class ErrorInfo(Contract):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False
    request_id: str = ""


class Job(Contract):
    job_id: str
    operation: str
    project_id: str
    status: Literal["queued", "running", "succeeded", "partial_success", "failed"] = "queued"
    stage: str = "queued"
    progress: int = Field(default=0, ge=0, le=100)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    result: dict[str, Any] | None = None
    result_url: str | None = None
    error: ErrorInfo | None = None
    context_ref: dict[str, Any] | None = None
    child_job_ids: list[str] = Field(default_factory=list)


class JobReceipt(Contract):
    job_id: str
    status_url: str
    operation: str
    project_id: str
