"""The refresh queue, the job read, and a knowledge base's refresh history and schedule."""

from core.cqrs.contracts import Job
from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from wireup import Injected
from wireup.integration.fastapi import WireupRoute

from studio.knowledge_base.http.api_models import RefreshRunsResult, ScheduleReport, error_response
from studio.knowledge_base.http.service import KnowledgeBaseRegistry
from studio.knowledge_base.settings import Schedule


def conflict_response(name: str, job_id: int) -> JSONResponse:
    """The 409 a live refresh answers: the uniform `{error, detail}`, and the id of the job to watch.

    Args:
        name: The registered name.
        job_id: The live refresh job's id.

    Returns:
        The response.
    """
    return error_response(409, "refresh_in_progress", f"a refresh of {name!r} is already queued or underway.", extra={"job_id": job_id})


class RefreshController:
    """Enqueuing a refresh, reading a job, and a knowledge base's refresh history and schedule.

    It serves routes under both `/api/jobs` and `/api/knowledge-bases`.
    No refresh runs in this process: the background worker claims each job and runs it.
    """

    router = APIRouter(prefix="/api", tags=["refresh"], route_class=WireupRoute)

    @router.post("/knowledge-bases/{name}/refresh", response_model=None, summary="Enqueue a refresh", operation_id="startRefresh")
    async def start_refresh(self, name: str, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> JSONResponse:
        """Enqueue one `kb.refresh` job, answering 202 with the id to watch, or 409 when one is live."""
        answer = await knowledge_base_registry.start_refresh_for(name)
        if not answer.created:
            return conflict_response(name, answer.job_id)
        return JSONResponse(status_code=202, content={"job_id": answer.job_id}, headers={"Location": f"/api/jobs/{answer.job_id}"})

    @router.get("/jobs/{job_id}", response_model=None, summary="Read one job", operation_id="readJob")
    async def read_job(self, job_id: int, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> Job:
        """One job whole, by id."""
        return await knowledge_base_registry.job_detail(job_id)

    @router.get("/knowledge-bases/{name}/refresh-runs", response_model=None, summary="List the refresh runs", operation_id="listRefreshRuns")
    async def list_refresh_runs(self, name: str, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> RefreshRunsResult:
        """The last 20 finished refreshes of a registered knowledge base, newest first."""
        return await knowledge_base_registry.refresh_runs(name)

    @router.put("/knowledge-bases/{name}/schedule", response_model=None, summary="Declare a cadence", operation_id="setSchedule")
    async def set_schedule(self, name: str, payload: Schedule, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> ScheduleReport:
        """Declare the registered knowledge base's refresh schedule."""
        await run_in_threadpool(knowledge_base_registry.write_schedule, name, payload)
        return ScheduleReport(name=name, schedule=payload)

    @router.delete("/knowledge-bases/{name}/schedule", response_model=None, summary="Clear the cadence", operation_id="clearSchedule")
    async def clear_schedule(self, name: str, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> ScheduleReport:
        """Clear the registered knowledge base's refresh schedule."""
        await run_in_threadpool(knowledge_base_registry.write_schedule, name, None)
        return ScheduleReport(name=name, schedule=None)
