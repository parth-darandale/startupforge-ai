"""Honest, injectable generation boundaries; no generator is bundled here."""
import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Header, Path, Query, Request
from pydantic import ValidationError

from ..modules.intelligence.contracts import ContextDraft, JobReceipt, ProjectContext
from ..modules.intelligence.errors import IntelligenceError
from ..modules.intelligence.interfaces import JobOutcome
from .contracts import (ContextHandoff, ContextReference, GenerationResult, ModuleCapability,
                        ModuleStatus, PitchDeckRequest, PosterRequest, ReportGenerationRequest,
                        TemplateDescriptor)
from .interfaces import IntegrationDependencies


def context_digest(context: ProjectContext) -> str:
    payload = context.model_dump(mode="json", exclude={"project_id", "context_version", "created_at", "content_hash"})
    draft = ContextDraft.model_validate(payload).model_dump(mode="json")
    canonical = json.dumps(draft, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def verify_context(context: ProjectContext, deps: IntegrationDependencies) -> ProjectContext:
    """Pin a full stored snapshot; never substitute the project's latest context."""
    if context.content_hash != context_digest(context):
        raise IntelligenceError("CONTEXT_HASH_MISMATCH", "The supplied context does not match its content hash.", 409)
    saved = deps.repository.get_context(context.project_id, context.context_version)
    if saved.content_hash != context_digest(saved):
        raise IntelligenceError("STORED_CONTEXT_INVALID", "The saved context failed its integrity check.", 500)
    if context.model_dump(mode="json") != saved.model_dump(mode="json"):
        raise IntelligenceError("CONTEXT_SNAPSHOT_MISMATCH", "Supply the complete saved context without editing it.", 409,
                                details={"project_id": context.project_id, "context_version": context.context_version})
    if saved.critic.review_status == "blocked":
        raise IntelligenceError("CONTEXT_BLOCKED", "Resolve the blocked review before generating outputs.", 422)
    return saved


def capabilities(deps: IntegrationDependencies) -> list[ModuleCapability]:
    return [
        ModuleCapability(member=2, module="visual_studio", available=deps.adapters.visual_studio is not None,
                         required_adapter="VisualStudioAdapter", expected_folder="member2",
                         api_routes=["GET /api/v1/studio/templates", "POST /api/v1/studio/pitch-decks",
                                     "POST /api/v1/studio/posters"]),
        ModuleCapability(member=3, module="reports", available=deps.adapters.reports is not None,
                         required_adapter="ReportsAdapter", expected_folder="member3",
                         api_routes=["GET /api/v1/reports/templates", "POST /api/v1/reports/generations"]),
    ]


def require_adapter(deps: IntegrationDependencies, kind: str):
    is_report = kind == "report"
    adapter = deps.adapters.reports if is_report else deps.adapters.visual_studio
    if adapter is None:
        capability = capabilities(deps)[1 if is_report else 0]
        raise IntelligenceError("MODULE_NOT_AVAILABLE", f"Member {capability.member}'s {capability.module} module is not installed yet.",
                                503, details={"member": capability.member, "module": capability.module,
                                              "required_adapter": capability.required_adapter,
                                              "expected_folder": capability.expected_folder})
    return adapter


def read_templates(adapter, kind: str | None = None, *, report: bool = False) -> list[TemplateDescriptor]:
    try:
        templates = [TemplateDescriptor.model_validate(item.model_dump() if isinstance(item, TemplateDescriptor) else item) for item in
                     (adapter.templates() if report else adapter.templates(kind))]
    except (ValidationError, TypeError) as exc:
        raise IntelligenceError("ADAPTER_RESULT_INVALID", "The module returned invalid template metadata.", 502) from exc
    permitted = {"report"} if report else ({kind} if kind else {"pitch_deck", "poster"})
    if any(template.kind not in permitted for template in templates):
        raise IntelligenceError("ADAPTER_RESULT_INVALID", "The module returned a template for the wrong output kind.", 502)
    ids = [template.template_id for template in templates]
    if len(ids) != len(set(ids)):
        raise IntelligenceError("ADAPTER_RESULT_INVALID", "The module returned duplicate template IDs.", 502)
    return templates


def submit_generation(deps: IntegrationDependencies, kind: str, body, key: str, request_id: str) -> JobReceipt:
    context = verify_context(body.context, deps)
    adapter = require_adapter(deps, kind)
    templates = read_templates(adapter, None if kind == "report" else kind, report=kind == "report")
    options = body.options.model_dump(mode="json")
    selected = next((item for item in templates if item.template_id == options["template_id"]), None)
    if selected is None:
        raise IntelligenceError("TEMPLATE_NOT_FOUND", "Choose a template returned by this module's template endpoint.", 422)
    formats = options.get("formats", ["pptx" if kind == "pitch_deck" else "txt"])
    if any(fmt not in selected.supported_formats for fmt in formats):
        raise IntelligenceError("UNSUPPORTED_FORMAT", "This template does not support the requested export format.", 422,
                                details={"supported_formats": selected.supported_formats})
    adapter.validate_options(kind, options)
    reference = ContextReference.from_context(context)

    async def work(progress):
        await progress("generating_" + kind, 5)
        generation_context = context.model_copy(deep=True)
        try:
            raw_result = await adapter.generate(kind, generation_context, dict(options), progress)
            result = GenerationResult.model_validate(raw_result.model_dump() if isinstance(raw_result, GenerationResult) else raw_result)
        except (ValidationError, TypeError) as exc:
            raise IntelligenceError("ADAPTER_RESULT_INVALID", "The generator must return registered artifact/file IDs and context provenance.", 502) from exc
        if generation_context.model_dump(mode="json") != context.model_dump(mode="json"):
            raise IntelligenceError("ADAPTER_RESULT_INVALID", "The generator changed its immutable input context.", 502)
        if result.context_ref != reference or result.kind != kind:
            raise IntelligenceError("ADAPTER_RESULT_INVALID", "The output does not identify the requested context and output kind.", 502)
        return JobOutcome(result=result.model_dump(mode="json"))

    receipt = deps.jobs.submit("generate_" + kind, context.project_id, key, body.model_dump(mode="json"),
                               work, request_id)
    # Local submission and pinning are synchronous, before its asyncio task runs.
    # Final shared adapters must persist this reference atomically with submission.
    deps.pin_job_context(receipt.job_id, reference.model_dump(mode="json"))
    return receipt


def build_integration_router(deps: IntegrationDependencies) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Teammate integration"])

    @router.get("/integration/modules", response_model=ModuleStatus)
    async def modules():
        return ModuleStatus(modules=capabilities(deps))

    @router.get("/projects/{project_id}/contexts/{version}/handoff", response_model=ContextHandoff)
    async def handoff(project_id: str, version: int = Path(ge=1)):
        saved = deps.repository.get_context(project_id, version)
        if saved.content_hash != context_digest(saved):
            raise IntelligenceError("STORED_CONTEXT_INVALID", "The saved context failed its integrity check.", 500)
        return ContextHandoff(context=saved, context_ref=ContextReference.from_context(saved),
                              generation_eligible=saved.critic.review_status != "blocked", destinations=capabilities(deps))

    @router.get("/studio/templates")
    async def studio_templates(kind: Literal["pitch_deck", "poster"] | None = Query(default=None)):
        adapter = require_adapter(deps, kind or "pitch_deck")
        return {"templates": read_templates(adapter, kind)}

    @router.get("/reports/templates")
    async def report_templates():
        return {"templates": read_templates(require_adapter(deps, "report"), report=True)}

    @router.post("/studio/pitch-decks", status_code=202, response_model=JobReceipt)
    async def pitch_deck(body: PitchDeckRequest, request: Request, key: str = Header(alias="Idempotency-Key")):
        return submit_generation(deps, "pitch_deck", body, key, request.state.request_id)

    @router.post("/studio/posters", status_code=202, response_model=JobReceipt)
    async def poster(body: PosterRequest, request: Request, key: str = Header(alias="Idempotency-Key")):
        return submit_generation(deps, "poster", body, key, request.state.request_id)

    @router.post("/reports/generations", status_code=202, response_model=JobReceipt)
    async def report(body: ReportGenerationRequest, request: Request, key: str = Header(alias="Idempotency-Key")):
        return submit_generation(deps, "report", body, key, request.state.request_id)

    return router
