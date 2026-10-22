"""Shared local SQLite repository for projects, immutable contexts and jobs."""
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.modules.intelligence.contracts import AnalysisResult, ContextDraft, DebateSession, Job, ProjectContext, utc_now
from app.modules.intelligence.errors import IntelligenceError


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class SQLiteStore:
    def __init__(self, database_path: str):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS contexts (project_id TEXT, version INTEGER, payload TEXT NOT NULL,
                    PRIMARY KEY (project_id, version));
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, scope TEXT NOT NULL, token TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(scope, token));
                CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS debates (id TEXT PRIMARY KEY, payload TEXT NOT NULL, busy INTEGER NOT NULL DEFAULT 0);
            """)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(str(self.path), timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create_project(self, display_name: str, idea_text: str) -> dict[str, Any]:
        now = utc_now().isoformat()
        project = {"project_id": f"p_{uuid.uuid4().hex}", "display_name": display_name,
                   "draft_idea_text": idea_text, "revision": 1, "latest_context_version": 0,
                   "created_at": now, "updated_at": now}
        with self.lock, self.connection() as connection:
            connection.execute("INSERT INTO projects VALUES (?,?)", (project["project_id"], canonical(project)))
        return project

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise IntelligenceError("PROJECT_NOT_FOUND", "This project does not exist.", 404)
        return json.loads(row["payload"])

    def list_projects(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT payload FROM projects ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def update_project(self, project_id: str, expected_revision: int, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {key: value for key, value in changes.items() if key in {"display_name", "draft_idea_text"}}
        if not allowed:
            raise IntelligenceError("EMPTY_PROJECT_UPDATE", "Provide a project name or draft idea change.", 422)
        with self.lock, self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise IntelligenceError("PROJECT_NOT_FOUND", "This project does not exist.", 404)
            project = json.loads(row["payload"])
            actual = project["revision"]
            if actual != expected_revision:
                raise IntelligenceError("PROJECT_REVISION_CONFLICT", "The project metadata has changed.", 409,
                                        details={"expected": expected_revision, "actual": actual})
            project.update(allowed)
            project["revision"] = actual + 1
            project["updated_at"] = utc_now().isoformat()
            connection.execute("UPDATE projects SET payload=? WHERE id=?", (canonical(project), project_id))
        return project

    def list_contexts(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        self.get_project(project_id)
        with self.connection() as connection:
            rows = connection.execute("SELECT version,payload FROM contexts WHERE project_id=? ORDER BY version DESC LIMIT ?",
                                      (project_id, limit)).fetchall()
        summaries = []
        for row in rows:
            context = ProjectContext.model_validate_json(row["payload"])
            summaries.append({"context_version": row["version"], "content_hash": context.content_hash,
                             "created_at": context.created_at, "review_status": context.critic.review_status,
                             "project_name": context.idea.project_name})
        return summaries

    def commit_context(self, project_id: str, expected: int, draft: ContextDraft) -> ProjectContext:
        with self.lock, self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise IntelligenceError("PROJECT_NOT_FOUND", "This project does not exist.", 404)
            project = json.loads(row["payload"])
            actual = project["latest_context_version"]
            if actual != expected:
                raise IntelligenceError("CONTEXT_VERSION_CONFLICT", "The project has a newer context version.", 409,
                                        details={"expected": expected, "actual": actual})
            payload = draft.model_dump(mode="json")
            context = ProjectContext(**payload, project_id=project_id, context_version=actual + 1,
                                     created_at=utc_now(), content_hash=hashlib.sha256(canonical(payload).encode()).hexdigest())
            connection.execute("INSERT INTO contexts VALUES (?,?,?)",
                               (project_id, actual + 1, context.model_dump_json()))
            project.update(latest_context_version=actual + 1, revision=project["revision"] + 1,
                           updated_at=utc_now().isoformat())
            connection.execute("UPDATE projects SET payload=? WHERE id=?", (canonical(project), project_id))
        return context

    def get_context(self, project_id: str, version: int | None = None) -> ProjectContext:
        project = self.get_project(project_id)
        selected = version if version is not None else project["latest_context_version"]
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM contexts WHERE project_id=? AND version=?",
                                     (project_id, selected)).fetchone()
        if not row:
            raise IntelligenceError("CONTEXT_NOT_FOUND", "No saved context exists for this version.", 404)
        return ProjectContext.model_validate_json(row["payload"])

    def reserve_job(self, operation: str, project_id: str, token: str, payload: dict,
                    scope_suffix: str = "") -> tuple[Job, bool]:
        fingerprint = hashlib.sha256(canonical(payload).encode()).hexdigest()
        scope = f"{project_id}:{operation}:{scope_suffix}"
        with self.lock, self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload,fingerprint FROM jobs WHERE scope=? AND token=?",
                                     (scope, token)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise IntelligenceError("IDEMPOTENCY_CONFLICT", "This request key was already used for different input.", 409)
                return Job.model_validate_json(row["payload"]), False
            job = Job(job_id=f"j_{uuid.uuid4().hex}", operation=operation, project_id=project_id)
            connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?)",
                               (job.job_id, scope, token, fingerprint, job.model_dump_json()))
        return job, True

    def get_job(self, job_id: str) -> Job:
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise IntelligenceError("JOB_NOT_FOUND", "This job does not exist.", 404)
        return Job.model_validate_json(row["payload"])

    def update_job(self, job_id: str, **changes: Any) -> Job:
        with self.lock, self.connection() as connection:
            row = connection.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise IntelligenceError("JOB_NOT_FOUND", "This job does not exist.", 404)
            job = Job.model_validate_json(row["payload"])
            candidate = job.model_dump()
            candidate.update(changes, updated_at=utc_now())
            updated = Job.model_validate(candidate)
            connection.execute("UPDATE jobs SET payload=? WHERE id=?", (updated.model_dump_json(), job_id))
        return updated

    def list_jobs(self, project_id: str) -> list[Job]:
        with self.connection() as connection:
            rows = connection.execute("SELECT payload FROM jobs ORDER BY rowid DESC").fetchall()
        return [job for row in rows if (job := Job.model_validate_json(row["payload"])).project_id == project_id][:100]

    def save_analysis(self, result: AnalysisResult) -> None:
        with self.lock, self.connection() as connection:
            connection.execute("INSERT INTO analyses VALUES (?,?,?)",
                               (result.analysis_id, result.project_id, result.model_dump_json()))

    def get_analysis(self, analysis_id: str) -> AnalysisResult:
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM analyses WHERE id=?", (analysis_id,)).fetchone()
        if not row:
            raise IntelligenceError("ANALYSIS_NOT_FOUND", "This analysis does not exist.", 404)
        return AnalysisResult.model_validate_json(row["payload"])

    def save_debate(self, session: DebateSession) -> None:
        with self.lock, self.connection() as connection:
            connection.execute("INSERT INTO debates(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                               (session.debate_id, session.model_dump_json()))

    def get_debate(self, debate_id: str) -> DebateSession:
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM debates WHERE id=?", (debate_id,)).fetchone()
        if not row:
            raise IntelligenceError("DEBATE_NOT_FOUND", "This debate does not exist.", 404)
        return DebateSession.model_validate_json(row["payload"])

    def claim_debate(self, debate_id: str) -> None:
        with self.lock, self.connection() as connection:
            changed = connection.execute("UPDATE debates SET busy=1 WHERE id=? AND busy=0", (debate_id,))
            if changed.rowcount == 1:
                return
            row = connection.execute("SELECT busy FROM debates WHERE id=?", (debate_id,)).fetchone()
            if not row:
                raise IntelligenceError("DEBATE_NOT_FOUND", "This debate does not exist.", 404)
            if row["busy"]:
                raise IntelligenceError("DEBATE_BUSY", "Wait for the current debate turn to finish.", 409)

    def release_debate(self, debate_id: str) -> None:
        with self.connection() as connection:
            connection.execute("UPDATE debates SET busy=0 WHERE id=?", (debate_id,))

    def recover_interrupted_jobs(self) -> None:
        from app.modules.intelligence.contracts import ErrorInfo
        with self.connection() as connection:
            rows = connection.execute("SELECT id,payload FROM jobs").fetchall()
        for row in rows:
            job = Job.model_validate_json(row["payload"])
            if job.status in {"queued", "running"}:
                self.update_job(job.job_id, status="failed", stage="interrupted", error=ErrorInfo(
                    code="JOB_INTERRUPTED", message="The server restarted before this job finished. Start a new attempt.", retryable=True))
        with self.connection() as connection:
            connection.execute("UPDATE debates SET busy=0")
