"""Inject these boundary dependencies when mounting Member 1 in the final app."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from .contracts import AnalysisResult, DebateSession, JobReceipt

Progress = Callable[[str, int], Awaitable[None]]


@dataclass
class JobOutcome:
    result: dict[str, Any] | None = None
    result_url: str | None = None
    status: str = "succeeded"


class IntelligenceRepository(Protocol):
    def get_project(self, project_id: str) -> dict[str, Any]: ...
    def save_analysis(self, result: AnalysisResult) -> None: ...
    def get_analysis(self, analysis_id: str) -> AnalysisResult: ...
    def save_debate(self, session: DebateSession) -> None: ...
    def get_debate(self, debate_id: str) -> DebateSession: ...
    def claim_debate(self, debate_id: str) -> None: ...
    def release_debate(self, debate_id: str) -> None: ...


class JobRunner(Protocol):
    def publish_result_url(self, job_id: str, result_url: str) -> None: ...
    def submit(self, operation: str, project_id: str, key: str, payload: dict,
               work: Callable[[Progress], Awaitable[JobOutcome]], request_id: str,
               scope_suffix: str = "") -> JobReceipt: ...
