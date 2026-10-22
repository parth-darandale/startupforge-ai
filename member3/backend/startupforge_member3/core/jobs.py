import asyncio
import logging
from typing import Any, Awaitable, Callable

from app.modules.intelligence.contracts import ErrorInfo, JobReceipt
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.interfaces import JobOutcome, Progress
from .projects import SQLiteStore

LOGGER = logging.getLogger(__name__)


class LocalJobRunner:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.tasks: set[asyncio.Task] = set()
        self.semaphore = asyncio.Semaphore(2)

    def publish_result_url(self, job_id: str, result_url: str) -> None:
        self.store.update_job(job_id, result_url=result_url)

    def submit(self, operation: str, project_id: str, key: str, payload: dict,
               work: Callable[[Progress], Awaitable[JobOutcome]], request_id: str,
               scope_suffix: str = "") -> JobReceipt:
        if not key.strip() or len(key) > 200:
            raise IntelligenceError("INVALID_IDEMPOTENCY_KEY", "A request key between 1 and 200 characters is required.", 422)
        job, created = self.store.reserve_job(operation, project_id, key, payload, scope_suffix)
        if created:
            task = asyncio.create_task(self._run(job.job_id, work, request_id))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return JobReceipt(job_id=job.job_id, status_url=f"/api/v1/jobs/{job.job_id}",
                          operation=job.operation, project_id=project_id)

    async def _run(self, job_id: str, work: Callable, request_id: str) -> None:
        async def progress(stage: str, percent: int) -> None:
            self.store.update_job(job_id, stage=stage, progress=max(0, min(99, percent)))
        try:
            async with self.semaphore:
                self.store.update_job(job_id, status="running", stage="starting", progress=1)
                outcome = await work(progress)
                self.store.update_job(job_id, status=outcome.status,
                                      stage="partial_success" if outcome.status == "partial_success" else "completed", progress=100,
                                      result=outcome.result, result_url=outcome.result_url)
        except asyncio.CancelledError:
            self.store.update_job(job_id, status="failed", stage="interrupted", error=ErrorInfo(
                code="JOB_INTERRUPTED", message="The server stopped before this job finished.", retryable=True, request_id=request_id))
            raise
        except IntelligenceError as exc:
            self.store.update_job(job_id, status="failed", stage="failed", error=ErrorInfo(
                code=exc.code, message=exc.message, details=exc.details, retryable=exc.retryable, request_id=request_id))
        except Exception:
            LOGGER.exception("Job failed: %s, request %s", job_id, request_id)
            self.store.update_job(job_id, status="failed", stage="failed", error=ErrorInfo(
                code="INTERNAL_ERROR", message="This operation failed unexpectedly. Check the server log with its request ID.", request_id=request_id))

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
