"""The feature API. Mount build_router(dependencies) in the final backend."""
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Header, Request

from .contracts import (AnalysisRequest, AnalysisResult, DebateRequest, DebateSession,
                        DebateTurnRequest, ExtractionRequest, JobReceipt)
from .debate import DebateService
from .errors import IntelligenceError
from .figures import extract_figures
from .interfaces import IntelligenceRepository, JobOutcome, JobRunner
from .settings import Settings


@dataclass
class IntelligenceDependencies:
    settings: Settings
    service: Any
    repository: IntelligenceRepository
    jobs: JobRunner
    debates: DebateService
    configuration_error: IntelligenceError | None = None


def build_router(dependencies: IntelligenceDependencies) -> APIRouter:
    router = APIRouter(prefix="/api/v1/intelligence", tags=["Idea Intelligence"])

    def require_service():
        if dependencies.configuration_error:
            raise dependencies.configuration_error
        return dependencies.service

    @router.post("/extractions", response_model=JobReceipt, status_code=202)
    async def extract(body: ExtractionRequest, request: Request,
                      idempotency_key: str = Header(alias="Idempotency-Key")):
        dependencies.repository.get_project(body.project_id)

        async def work(progress):
            await progress("understanding", 20)
            result = await require_service().extract(body.idea_text)
            return JobOutcome(result=result.model_dump(mode="json"))

        return dependencies.jobs.submit("extract_idea", body.project_id, idempotency_key,
                                        body.model_dump(mode="json"), work, request.state.request_id)

    @router.post("/analyses", response_model=JobReceipt, status_code=202)
    async def analyze(body: AnalysisRequest, request: Request,
                      idempotency_key: str = Header(alias="Idempotency-Key")):
        dependencies.repository.get_project(body.project_id)

        async def work(progress):
            result = await require_service().analyze(body.project_id, body.idea, body.research_options, progress)
            figures = extract_figures(result.context_draft.evidence, result.context_draft.sources,
                                      idea=body.idea, options=body.research_options)
            result.figures = figures
            result.context_draft.figures = figures
            # Revalidate the boundary after enriching the pipeline output.
            result = AnalysisResult.model_validate(result.model_dump())
            dependencies.repository.save_analysis(result)
            return JobOutcome(result={"analysis_id": result.analysis_id},
                              result_url=f"/api/v1/intelligence/analyses/{result.analysis_id}")

        return dependencies.jobs.submit("analyze_idea", body.project_id, idempotency_key,
                                        body.model_dump(mode="json"), work, request.state.request_id)

    @router.get("/analyses/{analysis_id}", response_model=AnalysisResult)
    async def analysis(analysis_id: str):
        return dependencies.repository.get_analysis(analysis_id)

    @router.post("/debates", response_model=JobReceipt, status_code=202)
    async def create_debate(body: DebateRequest, request: Request,
                            idempotency_key: str = Header(alias="Idempotency-Key")):
        dependencies.repository.get_project(body.project_id)
        analysis = dependencies.repository.get_analysis(body.analysis_id)
        if analysis.project_id != body.project_id:
            raise IntelligenceError("ANALYSIS_PROJECT_MISMATCH", "The analysis belongs to another project.", 409)
        receipt: JobReceipt | None = None

        async def work(progress):
            session = dependencies.debates.create_session(body)
            url = f"/api/v1/intelligence/debates/{session.debate_id}"
            # Publish the transcript URL while agent-versus-agent work is running.
            if receipt:
                dependencies.jobs.publish_result_url(receipt.job_id, url)
            await dependencies.debates.prepare(session, progress)
            return JobOutcome(result={"debate_id": session.debate_id}, result_url=url)

        receipt = dependencies.jobs.submit("create_debate", body.project_id, idempotency_key,
                                           body.model_dump(mode="json"), work, request.state.request_id)
        return receipt

    @router.get("/debates/{debate_id}", response_model=DebateSession)
    async def get_debate(debate_id: str):
        return dependencies.repository.get_debate(debate_id)

    @router.post("/debates/{debate_id}/turns", response_model=JobReceipt, status_code=202)
    async def debate_turn(debate_id: str, body: DebateTurnRequest, request: Request,
                          idempotency_key: str = Header(alias="Idempotency-Key")):
        if not body.message.strip():
            raise IntelligenceError("EMPTY_ARGUMENT", "Write an argument before sending it.", 422)
        session = dependencies.repository.get_debate(debate_id)

        async def work(progress):
            updated = await dependencies.debates.reply(debate_id, body.message, progress)
            return JobOutcome(result={"debate_id": updated.debate_id},
                              result_url=f"/api/v1/intelligence/debates/{debate_id}")

        return dependencies.jobs.submit("debate_turn", session.project_id, idempotency_key,
                                        body.model_dump(mode="json"), work, request.state.request_id,
                                        scope_suffix=debate_id)

    return router
