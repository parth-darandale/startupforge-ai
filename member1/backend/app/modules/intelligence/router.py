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
from .models import ModelSelection
from .pipeline import IntelligenceService
from .providers import make_language_model
from .settings import Settings


@dataclass
class IntelligenceDependencies:
    settings: Settings
    service: Any
    repository: IntelligenceRepository
    jobs: JobRunner
    debates: DebateService
    configuration_error: IntelligenceError | None = None
    model_selection: ModelSelection | None = None


def build_router(dependencies: IntelligenceDependencies) -> APIRouter:
    router = APIRouter(prefix="/api/v1/intelligence", tags=["Idea Intelligence"])
    if dependencies.model_selection is None:
        dependencies.model_selection = ModelSelection(dependencies.settings)

    def require_service():
        if dependencies.configuration_error:
            raise dependencies.configuration_error
        return dependencies.service

    async def selected_service(model_id: str | None):
        current = require_service()
        selected = await dependencies.model_selection.resolve(model_id)
        if selected == dependencies.settings:
            return current
        return IntelligenceService(selected, make_language_model(selected), current.search, current.retriever)

    async def selected_debates(model_id: str | None):
        if model_id is None:
            return dependencies.debates
        service = await selected_service(model_id)
        if service is dependencies.service:
            return dependencies.debates
        return DebateService(service.settings, dependencies.repository, service.llm)

    def request_payload(body):
        payload = body.model_dump(mode="json")
        if payload.get("model_id") is None:
            payload.pop("model_id", None)
        return payload

    @router.get("/models")
    async def models():
        return await dependencies.model_selection.catalog()

    @router.post("/extractions", response_model=JobReceipt, status_code=202)
    async def extract(body: ExtractionRequest, request: Request,
                      idempotency_key: str = Header(alias="Idempotency-Key")):
        dependencies.repository.get_project(body.project_id)
        dependencies.model_selection.validate(body.model_id)

        async def work(progress):
            await progress("understanding", 20)
            result = await (await selected_service(body.model_id)).extract(body.idea_text)
            return JobOutcome(result=result.model_dump(mode="json"))

        return dependencies.jobs.submit("extract_idea", body.project_id, idempotency_key,
                                        request_payload(body), work, request.state.request_id)

    @router.post("/analyses", response_model=JobReceipt, status_code=202)
    async def analyze(body: AnalysisRequest, request: Request,
                      idempotency_key: str = Header(alias="Idempotency-Key")):
        dependencies.repository.get_project(body.project_id)
        dependencies.model_selection.validate(body.model_id)

        async def work(progress):
            result = await (await selected_service(body.model_id)).analyze(body.project_id, body.idea, body.research_options, progress)
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
                                        request_payload(body), work, request.state.request_id)

    @router.get("/analyses/{analysis_id}", response_model=AnalysisResult)
    async def analysis(analysis_id: str):
        return dependencies.repository.get_analysis(analysis_id)

    @router.post("/debates", response_model=JobReceipt, status_code=202)
    async def create_debate(body: DebateRequest, request: Request,
                            idempotency_key: str = Header(alias="Idempotency-Key")):
        dependencies.repository.get_project(body.project_id)
        dependencies.model_selection.validate(body.model_id)
        analysis = dependencies.repository.get_analysis(body.analysis_id)
        if analysis.project_id != body.project_id:
            raise IntelligenceError("ANALYSIS_PROJECT_MISMATCH", "The analysis belongs to another project.", 409)
        receipt: JobReceipt | None = None

        async def work(progress):
            debates = await selected_debates(body.model_id)
            session = debates.create_session(body)
            url = f"/api/v1/intelligence/debates/{session.debate_id}"
            # Publish the transcript URL while agent-versus-agent work is running.
            if receipt:
                dependencies.jobs.publish_result_url(receipt.job_id, url)
            await debates.prepare(session, progress)
            return JobOutcome(result={"debate_id": session.debate_id}, result_url=url)

        receipt = dependencies.jobs.submit("create_debate", body.project_id, idempotency_key,
                                           request_payload(body), work, request.state.request_id)
        return receipt

    @router.get("/debates/{debate_id}", response_model=DebateSession)
    async def get_debate(debate_id: str):
        return dependencies.repository.get_debate(debate_id)

    @router.post("/debates/{debate_id}/resume", response_model=JobReceipt, status_code=202)
    async def resume_debate(debate_id: str, request: Request,
                            idempotency_key: str = Header(alias="Idempotency-Key")):
        session = dependencies.repository.get_debate(debate_id)
        receipt: JobReceipt | None = None

        async def work(progress):
            url = f"/api/v1/intelligence/debates/{debate_id}"
            if receipt:
                dependencies.jobs.publish_result_url(receipt.job_id, url)
            debates = await selected_debates(session.model_id)
            updated = await debates.resume(debate_id, progress)
            return JobOutcome(result={"debate_id": updated.debate_id}, result_url=url)

        receipt = dependencies.jobs.submit("resume_debate", session.project_id, idempotency_key,
                                           {"debate_id": debate_id}, work, request.state.request_id,
                                           scope_suffix=debate_id)
        return receipt

    @router.post("/debates/{debate_id}/turns", response_model=JobReceipt, status_code=202)
    async def debate_turn(debate_id: str, body: DebateTurnRequest, request: Request,
                          idempotency_key: str = Header(alias="Idempotency-Key")):
        if not body.message.strip():
            raise IntelligenceError("EMPTY_ARGUMENT", "Write an argument before sending it.", 422)
        session = dependencies.repository.get_debate(debate_id)

        async def work(progress):
            debates = await selected_debates(session.model_id)
            updated = await debates.reply(debate_id, body.message, progress)
            return JobOutcome(result={"debate_id": updated.debate_id},
                              result_url=f"/api/v1/intelligence/debates/{debate_id}")

        return dependencies.jobs.submit("debate_turn", session.project_id, idempotency_key,
                                        body.model_dump(mode="json"), work, request.state.request_id,
                                        scope_suffix=debate_id)

    return router
