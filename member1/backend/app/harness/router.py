"""Minimal workspace/job routes for development. Member 3 replaces these later."""
from fastapi import APIRouter, Query
from pydantic import Field

from ..modules.intelligence.contracts import Contract, ContextDraft, Job, ProjectContext
from .store import SQLiteStore


class CreateProject(Contract):
    display_name: str = Field(min_length=1, max_length=200)
    idea_text: str = Field(min_length=10, max_length=12000)


class CommitContext(Contract):
    expected_latest_version: int = Field(ge=0)
    context: ContextDraft


def build_harness_router(store: SQLiteStore) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Standalone harness"])

    @router.post("/projects", status_code=201)
    async def create_project(body: CreateProject):
        return store.create_project(body.display_name, body.idea_text)

    @router.get("/projects")
    async def projects(limit: int = Query(default=20, ge=1, le=100)):
        return {"items": store.list_projects(limit), "next_cursor": None}

    @router.get("/projects/{project_id}")
    async def project(project_id: str):
        return store.get_project(project_id)

    @router.post("/projects/{project_id}/contexts", status_code=201, response_model=ProjectContext)
    async def context_commit(project_id: str, body: CommitContext):
        return store.commit_context(project_id, body.expected_latest_version, body.context)

    @router.get("/projects/{project_id}/context", response_model=ProjectContext)
    async def context_latest(project_id: str):
        return store.get_context(project_id)

    @router.get("/projects/{project_id}/contexts/{version}", response_model=ProjectContext)
    async def context_version(project_id: str, version: int):
        return store.get_context(project_id, version)

    @router.get("/jobs/{job_id}", response_model=Job)
    async def job(job_id: str):
        return store.get_job(job_id)

    @router.get("/projects/{project_id}/jobs")
    async def jobs(project_id: str):
        store.get_project(project_id)
        return {"jobs": store.list_jobs(project_id), "next_cursor": None}

    return router
