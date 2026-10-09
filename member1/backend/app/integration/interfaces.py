"""Implement these interfaces in the finished teammate modules and inject them."""
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from ..modules.intelligence.contracts import ProjectContext
from ..modules.intelligence.interfaces import JobRunner, Progress
from .contracts import GenerationResult, TemplateDescriptor


class ContextRepository(Protocol):
    def get_context(self, project_id: str, version: int | None = None) -> ProjectContext: ...


class VisualStudioAdapter(Protocol):
    def templates(self, kind: Literal["pitch_deck", "poster"] | None = None) -> list[TemplateDescriptor]: ...
    def validate_options(self, kind: str, options: dict[str, Any]) -> None: ...
    async def generate(self, kind: str, context: ProjectContext, options: dict[str, Any],
                       progress: Progress) -> GenerationResult: ...


class ReportsAdapter(Protocol):
    def templates(self) -> list[TemplateDescriptor]: ...
    def validate_options(self, kind: str, options: dict[str, Any]) -> None: ...
    async def generate(self, kind: str, context: ProjectContext, options: dict[str, Any],
                       progress: Progress) -> GenerationResult: ...


@dataclass(frozen=True)
class IntegrationAdapters:
    visual_studio: VisualStudioAdapter | None = None
    reports: ReportsAdapter | None = None


@dataclass(frozen=True)
class IntegrationDependencies:
    repository: ContextRepository
    jobs: JobRunner
    adapters: IntegrationAdapters
    pin_job_context: Callable[[str, dict[str, Any]], None]
