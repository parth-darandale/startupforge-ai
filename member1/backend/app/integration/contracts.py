"""Additive connector contracts; the shared ProjectContext stays at schema 1.0."""
from typing import Any, Literal

from pydantic import Field, model_validator

from ..modules.intelligence.contracts import Contract, ProjectContext

ArtifactKind = Literal["pitch_deck", "poster", "report"]


class ContextReference(Contract):
    project_id: str = Field(min_length=1)
    context_version: int = Field(ge=1)
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_context(cls, context: ProjectContext) -> "ContextReference":
        return cls(project_id=context.project_id, context_version=context.context_version,
                   context_hash=context.content_hash)


class TemplateDescriptor(Contract):
    template_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    kind: ArtifactKind
    supported_formats: list[str] = Field(min_length=1, max_length=10)
    supported_options: dict[str, Any] = Field(default_factory=dict)


class PitchDeckOptions(Contract):
    template_id: str = Field(min_length=1, max_length=100)
    slide_count: int = Field(ge=1, le=100)
    tone: str = Field(min_length=1, max_length=100)


class PosterOptions(Contract):
    template_id: str = Field(min_length=1, max_length=100)
    audience: str = Field(min_length=1, max_length=500)


class ReportOptions(Contract):
    template_id: str = Field(min_length=1, max_length=100)
    formats: list[Literal["docx", "pdf"]] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def unique_formats(self) -> "ReportOptions":
        if len(self.formats) != len(set(self.formats)):
            raise ValueError("Requested formats must be unique")
        return self


class PitchDeckRequest(Contract):
    context: ProjectContext
    options: PitchDeckOptions


class PosterRequest(Contract):
    context: ProjectContext
    options: PosterOptions


class ReportGenerationRequest(Contract):
    context: ProjectContext
    options: ReportOptions


class GenerationResult(Contract):
    kind: ArtifactKind
    context_ref: ContextReference
    artifact_ids: list[str] = Field(min_length=1, max_length=100)
    file_ids: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def registered_ids(self) -> "GenerationResult":
        for values in (self.artifact_ids, self.file_ids):
            if len(values) != len(set(values)) or any(not value.strip() for value in values):
                raise ValueError("Registered artifact and file IDs must be nonempty and unique")
        return self


class ModuleCapability(Contract):
    member: int
    module: str
    available: bool
    required_adapter: str
    expected_folder: str
    api_routes: list[str]


class ModuleStatus(Contract):
    schema_version: Literal["1.0"] = "1.0"
    modules: list[ModuleCapability]


class ContextHandoff(Contract):
    context: ProjectContext
    context_ref: ContextReference
    generation_eligible: bool
    destinations: list[ModuleCapability]
