"""Integrated StartupForge host with shared workspace and three feature modules."""
from contextlib import asynccontextmanager
import re
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .integration.interfaces import IntegrationAdapters, IntegrationDependencies
from .integration.router import build_integration_router
from .modules.intelligence.debate import DebateService
from .modules.intelligence.errors import IntelligenceError
from .modules.intelligence.pipeline import build_service
from .modules.intelligence.router import IntelligenceDependencies, build_router
from .modules.intelligence.settings import BACKEND_ROOT, Settings


IMPLEMENTATION_ROOT = BACKEND_ROOT.parents[1]
for module_backend in (IMPLEMENTATION_ROOT / "member2" / "backend", IMPLEMENTATION_ROOT / "member3" / "backend"):
    if module_backend.is_dir() and str(module_backend) not in sys.path:
        sys.path.insert(0, str(module_backend))

from startupforge_member2.visual_studio import VisualStudioAdapter
from startupforge_member3.core.artifacts import ArtifactStore
from startupforge_member3.core.jobs import LocalJobRunner
from startupforge_member3.core.projects import SQLiteStore
from startupforge_member3.core.router import WorkspaceDependencies, build_workspace_router
from startupforge_member3.reports import ReportsAdapter


def create_app(settings: Settings | None = None, *, integration_adapters: IntegrationAdapters | None = None,
               artifact_storage_root: str | Path | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    path = Path(config.database_path)
    if not path.is_absolute():
        path = BACKEND_ROOT / path
    store = SQLiteStore(str(path))
    runner = LocalJobRunner(store)
    artifacts = ArtifactStore(path, artifact_storage_root or
                              IMPLEMENTATION_ROOT / "member3" / "backend" / "data" / "files")
    modules = integration_adapters or IntegrationAdapters(
        visual_studio=VisualStudioAdapter(artifacts), reports=ReportsAdapter(artifacts))
    configuration_error = None
    try:
        service = build_service(config)
    except IntelligenceError as exc:
        service, configuration_error = None, exc
    debate = DebateService(config, store, service.llm if service else None)
    dependencies = IntelligenceDependencies(config, service, store, runner, debate, configuration_error)
    connections = IntegrationDependencies(store, runner, modules,
                                          lambda job_id, reference: store.update_job(job_id, context_ref=reference))
    workspace = WorkspaceDependencies(store, artifacts, runner, modules,
                                      lambda job_id, reference: store.update_job(job_id, context_ref=reference),
                                      service, configuration_error)

    @asynccontextmanager
    async def lifespan(app):
        store.recover_interrupted_jobs()
        yield
        await runner.close()

    app = FastAPI(title="StartupForge", version="1.0.0", lifespan=lifespan,
                  description="Integrated workspace for idea research, pitch outputs and startup reports.")
    app.state.intelligence = dependencies
    app.state.store = store
    app.state.runner = runner
    app.state.integration = connections
    app.state.artifacts = artifacts
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["GET", "POST", "PUT", "PATCH"], allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"])

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request.state.request_id = supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", supplied) else f"req_{uuid.uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def envelope(request, code, message, details=None, retryable=False):
        return {"error": {"code": code, "message": message, "details": details or {},
                          "retryable": retryable, "request_id": getattr(request.state, "request_id", "")}}

    @app.exception_handler(IntelligenceError)
    async def intelligence_error(request: Request, exc: IntelligenceError):
        return JSONResponse(envelope(request, exc.code, exc.message, exc.details, exc.retryable), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        fields = [{"path": ".".join(str(x) for x in item["loc"]), "message": item["msg"], "type": item["type"]}
                  for item in exc.errors()]
        return JSONResponse(envelope(request, "VALIDATION_ERROR", "Check the highlighted input fields.", {"fields": fields}), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse(envelope(request, "HTTP_ERROR", str(exc.detail)), status_code=exc.status_code)

    @app.get("/api/v1/health", tags=["Standalone harness"])
    async def health():
        return {"status": "ok", "mode": config.app_mode, "schema_version": "1.0",
                "modules": ["intelligence", "visual_studio" if modules.visual_studio else None,
                            "reports" if modules.reports else None, "workspace"],
                "model": config.llm_model if config.app_mode == "live" else None,
                "providers": {"llm": getattr(service.llm, "name", "unconfigured") if service else "unconfigured",
                              "search": getattr(service.search, "name", "unconfigured") if service else "unconfigured",
                              "retrieval": getattr(service.retriever, "method", "unconfigured") if service else "unconfigured",
                              **({"discovery": getattr(config, "web_discovery_provider", "bing_news")}
                                 if config.app_mode == "live" and config.search_provider == "web" and configuration_error is None else {})},
                "capabilities": {"debate": True, "integration_endpoints": True,
                                 "pitch_decks": modules.visual_studio is not None,
                                 "posters": modules.visual_studio is not None,
                                 "reports": modules.reports is not None,
                                 "live_configured": config.app_mode == "live" and configuration_error is None},
                "configuration_message": configuration_error.message if configuration_error else None}

    app.include_router(build_router(dependencies))
    app.include_router(build_integration_router(connections))
    app.include_router(build_workspace_router(workspace))

    # After npm build, the standalone backend can host the feature UI on one port.
    frontend_dist = BACKEND_ROOT.parent / "frontend" / "dist"
    if frontend_dist.is_dir():
        if (frontend_dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="frontend-assets")

        @app.get("/", include_in_schema=False)
        async def frontend():
            return FileResponse(frontend_dist / "index.html")

    return app


app = create_app()
