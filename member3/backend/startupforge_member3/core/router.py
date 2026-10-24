"""Shared artifact editing, versioned rendering and safe file downloads."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.integration.contracts import (PitchDeckOptions, PosterOptions, ReportOptions)
from app.integration.interfaces import IntegrationAdapters
from app.modules.intelligence.contracts import ContextDraft, IdeaSpec, Job, JobReceipt, ProjectContext, ResearchOptions
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.interfaces import JobOutcome, JobRunner
from .projects import SQLiteStore
from app.modules.intelligence.figures import extract_figures

from .artifacts import ArtifactStore


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EditContentRequest(RequestModel):
    base_revision: int = Field(ge=1)
    content: dict[str, Any]


class CreateProjectRequest(RequestModel):
    display_name: str = Field(min_length=1, max_length=200)
    idea_text: str = Field(min_length=10, max_length=12000)


class CommitContextRequest(RequestModel):
    expected_latest_version: int = Field(ge=0)
    context: ContextDraft


class RenderRequest(RequestModel):
    content_revision: int = Field(ge=1)
    formats: list[str] = Field(min_length=1, max_length=4)


class PatchProjectRequest(RequestModel):
    expected_revision: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    draft_idea_text: str | None = Field(default=None, min_length=10, max_length=12000)
    draft_idea: IdeaSpec | None = None

    @model_validator(mode="after")
    def has_changes(self):
        if self.display_name is None and self.draft_idea_text is None and self.draft_idea is None:
            raise ValueError("Provide a project name or draft idea change")
        return self


class CombinedRunRequest(RequestModel):
    expected_latest_version: int = Field(ge=0)
    idea_text: str = Field(min_length=10, max_length=12000)
    outputs: list[Literal["pitch_deck", "poster", "report"]] = Field(min_length=1, max_length=3)
    output_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    research_options: ResearchOptions = Field(default_factory=ResearchOptions)

    @model_validator(mode="after")
    def unique_outputs(self):
        if len(self.outputs) != len(set(self.outputs)):
            raise ValueError("Output kinds must be unique")
        if set(self.output_options) - set(self.outputs):
            raise ValueError("Options can only be supplied for requested outputs")
        return self


@dataclass(frozen=True)
class WorkspaceDependencies:
    projects: SQLiteStore
    artifacts: ArtifactStore
    jobs: JobRunner
    adapters: IntegrationAdapters
    pin_job_context: Any
    intelligence_service: Any = None
    intelligence_error: IntelligenceError | None = None


def _adapter(deps: WorkspaceDependencies, kind: str):
    adapter = deps.adapters.reports if kind == "report" else deps.adapters.visual_studio
    if adapter is None:
        raise IntelligenceError("MODULE_NOT_AVAILABLE", f"The {kind} renderer is not available.", 503)
    return adapter


def _validate_content(adapter, kind: str, content: dict[str, Any], context):
    if kind == "report":
        return adapter.validate_content(content, context)
    return adapter.validate_content(kind, content, context)


def _render(adapter, kind: str, content: dict[str, Any], context, fmt: str):
    if kind == "report":
        return adapter.render(content, context, fmt)
    return adapter.render(kind, content, context, fmt)


def build_workspace_router(deps: WorkspaceDependencies) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Project workspace and artifacts"])

    @router.post("/projects", status_code=201)
    async def create_project(body: CreateProjectRequest):
        return deps.projects.create_project(body.display_name, body.idea_text)

    @router.get("/projects")
    async def projects(limit: int = Query(default=20, ge=1, le=100)):
        return {"items": deps.projects.list_projects(limit), "next_cursor": None}

    @router.get("/projects/{project_id}")
    async def project(project_id: str):
        return deps.projects.get_project(project_id)

    @router.post("/projects/{project_id}/contexts", status_code=201, response_model=ProjectContext)
    async def commit_context(project_id: str, body: CommitContextRequest):
        return deps.projects.commit_context(project_id, body.expected_latest_version, body.context)

    @router.get("/projects/{project_id}/context", response_model=ProjectContext)
    async def latest_context(project_id: str):
        return deps.projects.get_context(project_id)

    @router.get("/projects/{project_id}/contexts/{version}", response_model=ProjectContext)
    async def context_version(project_id: str, version: int):
        return deps.projects.get_context(project_id, version)

    @router.get("/jobs/{job_id}", response_model=Job)
    async def job(job_id: str):
        return deps.projects.get_job(job_id)

    @router.get("/projects/{project_id}/jobs")
    async def project_jobs(project_id: str, operation: str | None = Query(default=None, min_length=1, max_length=80),
                           debate_id: str | None = Query(default=None, min_length=1, max_length=100)):
        deps.projects.get_project(project_id)
        if debate_id and operation != "debate_turn":
            raise IntelligenceError("INVALID_JOB_FILTER", "A debate ID filter requires the debate_turn operation.", 422)
        return {"jobs": deps.projects.list_jobs(project_id, operation, debate_id), "next_cursor": None}

    @router.patch("/projects/{project_id}")
    async def patch_project(project_id: str, body: PatchProjectRequest):
        changes = {key: value for key, value in body.model_dump(exclude={"expected_revision"}, exclude_none=True).items()}
        return deps.projects.update_project(project_id, body.expected_revision, changes)

    @router.get("/projects/{project_id}/contexts")
    async def project_contexts(project_id: str, limit: int = Query(default=100, ge=1, le=100)):
        return {"items": deps.projects.list_contexts(project_id, limit), "next_cursor": None}

    @router.post("/projects/{project_id}/runs", status_code=202, response_model=JobReceipt)
    async def combined_run(project_id: str, body: CombinedRunRequest, request: Request,
                           key: str = Header(alias="Idempotency-Key")):
        deps.projects.get_project(project_id)
        if deps.intelligence_error:
            raise deps.intelligence_error
        if deps.intelligence_service is None:
            raise IntelligenceError("INTELLIGENCE_NOT_AVAILABLE", "The idea intelligence module is not configured.", 503)
        prepared: dict[str, tuple[Any, dict[str, Any]]] = {}
        defaults = {
            "pitch_deck": {"slide_count": 10, "tone": "professional"},
            "poster": {"audience": "Potential customers and reviewers"},
            "report": {"formats": ["docx"]},
        }
        for output_kind in body.outputs:
            adapter = deps.adapters.reports if output_kind == "report" else deps.adapters.visual_studio
            if adapter is None:
                raise IntelligenceError("MODULE_NOT_AVAILABLE", f"The {output_kind} module is not installed.", 503)
            raw_options = {**defaults[output_kind], **body.output_options.get(output_kind, {})}
            templates = adapter.templates() if output_kind == "report" else adapter.templates(output_kind)
            if not templates:
                raise IntelligenceError("TEMPLATE_NOT_FOUND", f"No {output_kind} template is installed.", 503)
            raw_options.setdefault("template_id", templates[0].template_id)
            try:
                option_model = {"pitch_deck": PitchDeckOptions, "poster": PosterOptions,
                                "report": ReportOptions}[output_kind]
                options = option_model.model_validate(raw_options).model_dump(mode="json")
            except Exception as exc:
                raise IntelligenceError("INVALID_OUTPUT_OPTIONS", f"Options for {output_kind} are invalid.", 422) from exc
            selected = next((item for item in templates if item.template_id == options["template_id"]), None)
            if selected is None:
                raise IntelligenceError("TEMPLATE_NOT_FOUND", f"Choose an installed {output_kind} template.", 422)
            formats = options.get("formats", ["pptx" if output_kind == "pitch_deck" else "txt"])
            if any(fmt not in selected.supported_formats for fmt in formats):
                raise IntelligenceError("UNSUPPORTED_FORMAT", f"The selected {output_kind} template does not support all requested formats.", 422,
                                        details={"supported_formats": selected.supported_formats})
            adapter.validate_options(output_kind, options)
            prepared[output_kind] = (adapter, options)
        payload = body.model_dump(mode="json") | {"project_id": project_id}
        parent_receipt: dict[str, JobReceipt] = {}

        async def work(parent_progress):
            await parent_progress("extracting_idea", 5)
            idea = await deps.intelligence_service.extract(body.idea_text)
            await parent_progress("research_and_analysis", 18)
            async def analysis_progress(stage: str, percent: int):
                await parent_progress(f"research_{stage}", 18 + int(max(0, min(100, percent)) * 0.32))
            analysis = await deps.intelligence_service.analyze(project_id, idea, body.research_options, analysis_progress)
            figures = extract_figures(analysis.context_draft.evidence, analysis.context_draft.sources,
                                      idea=idea, options=body.research_options)
            analysis.figures = figures
            analysis.context_draft.figures = figures
            deps.projects.save_analysis(analysis)
            await parent_progress("saving_context", 55)
            context = deps.projects.commit_context(project_id, body.expected_latest_version, analysis.context_draft)
            if parent_receipt:
                deps.pin_job_context(parent_receipt["receipt"].job_id, {
                    "project_id": context.project_id, "context_version": context.context_version,
                    "context_hash": context.content_hash,
                })
            await parent_progress("starting_output_jobs", 62)
            child_ids: list[str] = []

            async def generate_child(output_kind: str, adapter: Any, options: dict[str, Any]):
                operation = f"generate_{output_kind}"
                child_key = f"{parent_receipt['receipt'].job_id}:{output_kind}"
                child, created = deps.projects.reserve_job(operation, project_id, child_key,
                    {"context_version": context.context_version, "context_hash": context.content_hash, "options": options},
                    scope_suffix=parent_receipt["receipt"].job_id)
                child_ids.append(child.job_id)
                if not created and child.status == "succeeded":
                    return {"kind": output_kind, "job_id": child.job_id, "status": child.status}
                deps.projects.update_job(child.job_id, status="running", stage="starting", progress=1,
                                         context_ref={"project_id": context.project_id, "context_version": context.context_version,
                                                      "context_hash": context.content_hash})

                async def child_progress(stage: str, percent: int):
                    deps.projects.update_job(child.job_id, stage=stage, progress=max(0, min(99, percent)))
                    await parent_progress(f"{output_kind}_{stage}", min(96, 64 + percent // 4))
                try:
                    isolated_context = context.model_copy(deep=True)
                    result = await adapter.generate(output_kind, isolated_context, dict(options), child_progress)
                    if isolated_context.model_dump(mode="json") != context.model_dump(mode="json"):
                        raise IntelligenceError("ADAPTER_RESULT_INVALID", "The output module modified its immutable context.", 502)
                    result = result if isinstance(result, dict) else result.model_dump(mode="json")
                    if result.get("kind") != output_kind or result.get("context_ref") != {
                        "project_id": context.project_id, "context_version": context.context_version,
                        "context_hash": context.content_hash,
                    } or not result.get("artifact_ids") or not result.get("file_ids"):
                        raise IntelligenceError("ADAPTER_RESULT_INVALID", "The output did not register files for the saved context.", 502)
                    artifact_store = getattr(adapter, "artifacts", None)
                    if artifact_store is not None and hasattr(artifact_store, "bind_job"):
                        artifact_store.bind_job(result["artifact_ids"], child.job_id)
                    deps.projects.update_job(child.job_id, status="succeeded", stage="completed", progress=100,
                                             result=result)
                    return {"kind": output_kind, "job_id": child.job_id, "status": "succeeded",
                            "artifact_ids": result["artifact_ids"], "file_ids": result["file_ids"]}
                except IntelligenceError as exc:
                    deps.projects.update_job(child.job_id, status="failed", stage="failed", error={
                        "code": exc.code, "message": exc.message, "details": exc.details,
                        "retryable": exc.retryable, "request_id": request.state.request_id})
                    return {"kind": output_kind, "job_id": child.job_id, "status": "failed", "error": exc.code}
                except Exception as exc:
                    deps.projects.update_job(child.job_id, status="failed", stage="failed", error={
                        "code": "INTERNAL_ERROR", "message": "This output failed unexpectedly.",
                        "details": {}, "retryable": False, "request_id": request.state.request_id})
                    return {"kind": output_kind, "job_id": child.job_id, "status": "failed", "error": "INTERNAL_ERROR"}

            results = await asyncio.gather(*[
                generate_child(output_kind, adapter, options)
                for output_kind, (adapter, options) in prepared.items()
            ])
            successful = [item for item in results if item["status"] == "succeeded"]
            failed = [item for item in results if item["status"] != "succeeded"]
            result_payload = {"context_ref": {"project_id": context.project_id,
                                              "context_version": context.context_version,
                                              "context_hash": context.content_hash},
                              "successful_outputs": successful, "failed_outputs": failed,
                              "child_job_ids": child_ids}
            await parent_progress("completed", 99)
            if successful and failed:
                return JobOutcome(result=result_payload, status="partial_success")
            if failed:
                raise IntelligenceError("OUTPUTS_FAILED", "None of the requested output modules could complete.", 502,
                                        details={"context_version": context.context_version,
                                                 "failed_outputs": failed, "child_job_ids": child_ids})
            return JobOutcome(result=result_payload)

        receipt = deps.jobs.submit("combined_project_run", project_id, key, payload, work, request.state.request_id)
        parent_receipt["receipt"] = receipt
        return receipt

    @router.get("/projects/{project_id}/artifacts")
    async def project_artifacts(project_id: str, context_version: int | None = Query(default=None, ge=1),
                                kind: str | None = Query(default=None, pattern="^(pitch_deck|poster|report)$"),
                                limit: int = Query(default=100, ge=1, le=100)):
        deps.projects.get_project(project_id)
        items = deps.artifacts.list_artifacts(project_id, context_version=context_version, kind=kind, limit=limit)
        return {"items": items, "next_cursor": None}

    @router.get("/artifacts/{artifact_id}")
    async def artifact(artifact_id: str):
        return deps.artifacts.get_artifact(artifact_id)

    @router.get("/artifacts/{artifact_id}/content")
    async def artifact_content(artifact_id: str, revision: int | None = Query(default=None, ge=1)):
        return deps.artifacts.get_content(artifact_id, revision)

    @router.put("/artifacts/{artifact_id}/content")
    async def edit_artifact_content(artifact_id: str, body: EditContentRequest):
        manifest = deps.artifacts.get_artifact(artifact_id)
        context = deps.projects.get_context(manifest["project_id"], manifest["context_version"])
        adapter = _adapter(deps, manifest["kind"])
        normalized = _validate_content(adapter, manifest["kind"], body.content, context)
        updated = deps.artifacts.update_content(artifact_id, body.base_revision, normalized)
        return {**updated, "validation_findings": [], "manifest": deps.artifacts.get_artifact(artifact_id)}

    @router.post("/artifacts/{artifact_id}/renders", status_code=202, response_model=JobReceipt)
    async def render_artifact(artifact_id: str, body: RenderRequest, request: Request,
                              key: str = Header(alias="Idempotency-Key")):
        manifest = deps.artifacts.get_artifact(artifact_id)
        context = deps.projects.get_context(manifest["project_id"], manifest["context_version"])
        content_record = deps.artifacts.get_content(artifact_id, body.content_revision)
        adapter = _adapter(deps, manifest["kind"])
        normalized = _validate_content(adapter, manifest["kind"], content_record["content"], context)
        template_kind = manifest["kind"]
        if template_kind == "report":
            template_list = adapter.templates()
        else:
            template_list = adapter.templates(template_kind)
        supported = {fmt for template in template_list for fmt in template.supported_formats}
        if len(set(body.formats)) != len(body.formats) or any(fmt not in supported for fmt in body.formats):
            raise IntelligenceError("UNSUPPORTED_FORMAT", "One or more requested formats are unavailable.", 422,
                                    details={"supported_formats": sorted(supported)})
        reference = deps.artifacts.get_context_reference(artifact_id)
        receipt_holder: dict[str, JobReceipt] = {}

        async def work(progress):
            created_file_ids: list[str] = []
            for position, fmt in enumerate(body.formats, start=1):
                await progress(f"rendering_{fmt}", int(15 + (position - 1) * 65 / len(body.formats)))
                file_record = _render(adapter, template_kind, normalized, context, fmt)
                file_id = deps.artifacts.register_render(artifact_id, body.content_revision, file_record,
                                                        receipt_holder["receipt"].job_id)
                created_file_ids.append(file_id)
            return JobOutcome(result={"artifact_id": artifact_id, "content_revision": body.content_revision,
                                      "file_ids": created_file_ids})

        receipt = deps.jobs.submit("render_artifact", manifest["project_id"], key,
                                   {"artifact_id": artifact_id, **body.model_dump(mode="json")},
                                   work, request.state.request_id, scope_suffix=artifact_id)
        receipt_holder["receipt"] = receipt
        deps.pin_job_context(receipt.job_id, reference)
        return receipt

    @router.get("/files/{file_id}/download")
    async def download(file_id: str):
        record, data = deps.artifacts.get_file(file_id)
        filename = quote(record["filename"], safe="")
        headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                   "Content-Length": str(len(data)), "Cache-Control": "private, no-store"}
        return StreamingResponse(BytesIO(data), media_type=record["media_type"], headers=headers)

    return router
