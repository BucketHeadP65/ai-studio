"""The HTTP entry: `create_app` composes one application, its container and its lifespan.

    uv run uvicorn harness.main:create_app --factory --port 8770

The lifespan resolves the session from the container once per process. Every controller is mounted
here and nowhere else.
"""

from __future__ import annotations

import asyncio
import os

from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import logfire
import uvicorn

from core.ai.inspection.contracts import (
    ATTRIBUTE_LEAD_KIND,
    ATTRIBUTE_LEAD_POLICY,
    INSPECT_RUN_KIND,
    INSPECT_RUN_POLICY,
    SCAN_RUN_KIND,
    SCAN_RUN_POLICY,
    AttributeLead,
    AttributeLeadHandler,
    Dispatch,
    InspectRun,
    InspectRunHandler,
    ScanRun,
    ScanRunHandler,
    Sweep,
)
from core.cqrs.contracts import (
    Broadcaster,
    Clock,
    JobHandler,
    JobsAndQueries,
    MemoryOutbox,
    MessageBus,
    PollingWake,
    TopicSink,
    Wake,
    Worker,
    change_frames,
    utcnow,
)
from core.observability import PriceUpdater, Tracing, get_logger, start_price_updates, stop_tracing
from core.wiring.contracts import ContainerResolver, resolver_of
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from wireup.integration import fastapi as wireup_fastapi

from harness import session as session_state
from harness.adapters.contracts import DEFAULT_TIERS, ModelList, ModelTiers, TierSetting, prune_ended_spill_folders, set_tier, stored_tiers
from harness.composition import ensure_run_engine, restore_waiting_run, restore_waiting_runs, run_log_folders, serve_durable_runs
from harness.container import create_container
from harness.domain.contracts import Booted, SessionEvents, SessionTranscript, last_forgotten_at, live_session, logged_run_ids
from harness.features.authoring.controller import AuthoringController
from harness.features.browsing.controller import BrowsingController
from harness.features.inspection.controller import InspectionController
from harness.features.projects.controller import ProjectsController
from harness.features.running.contracts import RunCollaborators, RunRecords
from harness.features.running.controller import RunningController
from harness.features.scheduling.contracts import Scheduler
from harness.features.scheduling.controller import SchedulingController
from harness.features.selfauthoring.contracts import SelectionError, select_profile, selected_profile_name
from harness.features.selfauthoring.controller import SelfAuthoringController
from harness.features.sessions.contracts import SessionProjection, project_config, project_registry
from harness.features.sessions.controller import SessionsController
from harness.features.terminal.controller import TerminalController
from harness.session import Runtime, SessionPool
from harness.settings import (
    AI_FACTORY_STUDIO_ORIGINS,
    CHECKOUT,
    SPILL_DIR,
    HarnessSettings,
    InspectionSettings,
    RunSettings,
    SelfAuthoringSettings,
    StateSettings,
    custom_root,
)
from harness.tracing import POLLED

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.types import Lifespan
    from wireup import AsyncContainer

    from harness.features.running.contracts import RunArchive
    from harness.features.sessions.contracts import SetupConfig

_logger = get_logger(__name__)


IDLE_SWEEP_SECONDS = 5 * 60
"""How often, in seconds, the pool is swept for runtimes nobody has touched. A swept runtime costs its
next caller one hydration and nothing else."""


def sweep_idle_once(pool: SessionPool, topics: TopicSink) -> None:
    """Drop the pool's idle runtimes, once, and announce that the session list changed.

    A runtime in the middle of a turn is never dropped, and neither is the process's own session.

    Args:
        pool: The sessions this process holds.
        topics: Where the change to the session list is announced.
    """
    pool.evict_idle()
    topics.publish("sessions")


ARCHIVE_SWEEP_SECONDS = 24 * 60 * 60
"""How often, in seconds, the run archive is swept for expired records: once a day."""


def archive_sweep_once(archive: RunArchive) -> None:
    """Remove archived runs past their retention window, once.

    Args:
        archive: The run archive, which carries its own retention window.
    """
    removed = archive.sweep(archive.retention_days)
    if removed:
        _logger.info("archive sweep removed {n} ended run(s) past retention", n=removed)


SCHEDULE_TICK_SECONDS = 15
"""How often, in seconds, the ticker looks for a due schedule. A schedule's own cadence spaces its
fires. This sets only how soon a due fire is noticed."""


@dataclass(frozen=True)
class Driving:
    """How this process's background loop is driven."""

    wake: Wake
    """What shortens the loop's wait when another process has work for it."""
    name: str
    """What the loop calls itself where its periodics are logged."""
    clock: Clock = utcnow
    """The clock every periodic reads the time from."""


@dataclass(frozen=True)
class Inspecting:
    """The inspection's durable commands, independent handlers, dispatch and reconciliation."""

    jobs: JobsAndQueries
    inspect_run: JobHandler[InspectRun]
    attribute_lead: JobHandler[AttributeLead]
    scan_run: JobHandler[ScanRun]
    dispatch: Dispatch
    sweep: Sweep
    settings: InspectionSettings


def inspection_workers(inspecting: Inspecting, driving: Driving) -> tuple[Worker, Worker, Worker]:
    """Build three background workers, one each for inspecting runs, attributing leads and scanning runs.

    They share the same durable jobs, and each holds the leases of its own job type.

    Args:
        inspecting: The jobs and the handler for each job type.
        driving: The name and clock the workers run with.

    Returns:
        The workers for `inspect_run`, `attribute_lead` and `scan_run`, in that order.
    """
    workers = tuple(
        Worker(
            jobs=inspecting.jobs,
            outbox=MemoryOutbox(),
            bus=MessageBus({}, {}),
            wake=PollingWake(),
            name=f"{driving.name}:{lane}",
            clock=driving.clock,
            poll_seconds=inspecting.settings.job_poll_seconds,
        )
        for lane in (INSPECT_RUN_KIND, ATTRIBUTE_LEAD_KIND, SCAN_RUN_KIND)
    )
    rules, attribution, scans = workers
    rules.serve(INSPECT_RUN_KIND, InspectRun, inspecting.inspect_run, policy=INSPECT_RUN_POLICY)
    attribution.serve(ATTRIBUTE_LEAD_KIND, AttributeLead, inspecting.attribute_lead, policy=ATTRIBUTE_LEAD_POLICY)
    scans.serve(SCAN_RUN_KIND, ScanRun, inspecting.scan_run, policy=SCAN_RUN_POLICY)
    return rules, attribution, scans


def reconciliation_worker(inspecting: Inspecting, driving: Driving) -> Worker:
    """Build the background worker that runs the inspection's reconciliation sweep, apart from scheduling and dispatch.

    Args:
        inspecting: The jobs, the sweep and how often it runs.
        driving: The name and clock the worker runs with.

    Returns:
        The worker, with the sweep registered on it.
    """
    worker = Worker(
        jobs=inspecting.jobs,
        outbox=MemoryOutbox(),
        bus=MessageBus({}, {}),
        wake=PollingWake(),
        name=f"{driving.name}:reconciliation",
        clock=driving.clock,
    )

    async def inspection_sweep() -> None:
        await inspecting.sweep.tick()

    worker.every(inspecting.settings.sweep_seconds, inspection_sweep, name="inspection-sweep")
    return worker


def coordination_worker(pool: SessionPool, scheduler: Scheduler, run: RunCollaborators, driving: Driving, inspecting: Inspecting) -> Worker:
    """Build this process's timer worker: due schedules, the idle and archive sweeps, and inspection dispatch.

    Its jobs are `inspecting.jobs`, in the coordination schema of the sessions database. Its outbox and bus stay in
    memory and nothing subscribes to them. A `pg_notify` on the wake channel from another process shortens its wait.

    Args:
        pool: The sessions this process holds, which a schedule fires into and the idle sweep trims.
        scheduler: The schedules this process fires.
        run: The run records and topics a fired run writes to, and the engine settings it runs under.
        driving: The wake, name and clock the worker runs with.
        inspecting: The jobs, the dispatch and how often it runs.

    Returns:
        The worker, with every periodic registered on it.
    """
    worker = Worker(
        jobs=inspecting.jobs, outbox=MemoryOutbox(), bus=MessageBus({}, {}), wake=driving.wake, name=driving.name, clock=driving.clock
    )

    async def tick() -> None:
        await scheduler.tick(pool, run=run)

    async def idle_sweep() -> None:
        sweep_idle_once(pool, run.topics)

    async def archive() -> None:
        archive_sweep_once(run.records.archive)

    async def dispatch() -> None:
        await inspecting.dispatch.tick()

    worker.every(SCHEDULE_TICK_SECONDS, tick, name="schedules")
    worker.every(IDLE_SWEEP_SECONDS, idle_sweep, name="idle-sweep")
    worker.every(ARCHIVE_SWEEP_SECONDS, archive, name="archive-sweep")
    worker.every(inspecting.settings.dispatch_seconds, dispatch, name="inspection-dispatch")
    return worker


def _report_loop_exit(task: asyncio.Task[None]) -> None:
    """Log a background worker that stopped before the process did, which nothing restarts."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _logger.error("background worker {worker} stopped: {reason}", worker=task.get_name(), reason=repr(error), error=error)


async def _stop(task: asyncio.Task[None]) -> None:
    """Cancel a background task and wait for it to unwind, so shutdown leaves nothing running."""
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def _apply_setup(setup: SetupConfig, tiers: ModelTiers, custom: Path) -> None:
    """Apply the checkout's declared profile and orchestrator model where the owner set none, warning on an unusable name."""
    state = tiers.state
    if setup.profile is not None and selected_profile_name(state) is None:
        try:
            select_profile(setup.profile, root=custom, state=state)
        except SelectionError as exc:
            _logger.warning("project setup names an unusable profile {profile!r}: {reason}", profile=setup.profile, reason=str(exc))
    if setup.model is not None and "orchestrator" not in stored_tiers(tiers):
        try:
            set_tier(tiers, "orchestrator", TierSetting(model=setup.model, thinking=DEFAULT_TIERS["orchestrator"].thinking))
        except ValueError as exc:
            _logger.warning("project setup names an unusable model {model!r}: {reason}", model=setup.model, reason=str(exc))


CONTROLLERS: list[type] = [
    AuthoringController,
    BrowsingController,
    InspectionController,
    ProjectsController,
    RunningController,
    SchedulingController,
    SelfAuthoringController,
    SessionsController,
    TerminalController,
]
"""Every class whose methods are routes. Wireup resolves each from the container and includes its router."""


async def change_events(request: Request, topics: str = "") -> Response:
    """Every change notification this process hears, as an event stream.

    `topics=a,b` keeps only those roots and everything under them. No filter means every topic. The
    frames carry a topic and a time, never data: the reader refetches what it already reads. A
    process with no broadcaster (no database behind it) answers 503, which a browser's EventSource
    takes as final rather than reconnecting to.
    """
    wanted = frozenset(name.strip() for name in topics.split(",") if name.strip()) or None
    try:
        broadcaster = await resolver_of(request.app).get(Broadcaster)
    except (OSError, asyncpg.PostgresError):
        return JSONResponse({"ok": False, "message": "change notifications are not available. This process has no broadcaster."}, status_code=503)
    return StreamingResponse(change_frames(broadcaster, wanted), media_type="text/event-stream")


async def project_root_is_gone(request: Request, exc: Exception) -> JSONResponse:
    """Answer a session whose project directory this machine cannot open with 410, naming the directory.

    A 410 means the session is on record and only the checkout it was opened against is missing. Any
    other exception reaching here is answered as `any_failure_is_still_json` answers it.

    Args:
        request: The request that failed.
        exc: The exception it raised.

    Returns:
        A 410 response carrying the message and the missing root, or a 500 response for any other exception.
    """
    if not isinstance(exc, session_state.MissingProjectRootError):
        return await any_failure_is_still_json(request, exc)
    _logger.warning("a session on {path} names the project root {root}, which is gone", path=request.url.path, root=str(exc.root))
    return JSONResponse({"ok": False, "message": str(exc), "root": str(exc.root)}, status_code=410)


async def any_failure_is_still_json(request: Request, exc: Exception) -> JSONResponse:
    """Answer any unhandled exception with a JSON 500 response, never an HTML error page.

    The body names the exception's type and message, in the shape every route promises.

    Args:
        request: The request that failed.
        exc: The exception it raised.

    Returns:
        A 500 response whose body carries `ok`, `message`, `issues` and `valid`.
    """
    _logger.error("unhandled failure on {path}", path=request.url.path, error=exc)
    return JSONResponse({"ok": False, "message": f"{type(exc).__name__}: {exc}", "issues": [], "valid": False}, status_code=500)


def make_lifespan(container: AsyncContainer) -> Lifespan[FastAPI]:
    """The lifespan of an application resolving from `container`.

    Everything that outlives a request, such as the two pools, the change channel and the schedule
    store, is resolved from the container once, here.

    Args:
        container: What this application resolves from.

    Returns:
        The lifespan FastAPI enters before it serves and leaves when it stops.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Build the environment once per process: tracing, store, registry, port, agent, session."""
        del app
        (await container.get(Tracing)).start()
        state = await container.get(StateSettings)
        custom = custom_root(await container.get(SelfAuthoringSettings))
        async with AsyncExitStack() as stack:
            # Registered first so it unwinds last: every record the shutdown writes is flushed with the rest.
            stack.callback(stop_tracing)
            stop_price_updates = start_price_updates(await container.get(PriceUpdater))
            stack.push_async_callback(asyncio.to_thread, stop_price_updates)
            session = await container.get(Runtime)
            session_log = await container.get(SessionEvents)
            transcript = await container.get(SessionTranscript)
            records = await container.get(RunRecords)
            topics = await container.get(TopicSink)
            # The sweep first: a marker left by an older process becomes the boundary event resume is
            # about to honour, and any turn a crash left open is closed as abandoned, visibly.
            session_log.boot_sweep()
            await asyncio.to_thread(prune_ended_spill_folders, SPILL_DIR)
            events = session_log.events()
            live = live_session(events)
            # Before the first request, not on it: whoever opens AI Factory Studio should see the
            # conversation they were having. A remembered live session's own run is tried first.
            # `resume` falls back to the watermark walk when there is none or its snapshot is gone.
            resumed = await session_state.resume(
                session,
                transcript,
                run_id=live.last_run_id if live else None,
                run_ids=logged_run_ids(events),
                forgotten_after=last_forgotten_at(events),
            )
            if resumed is not None:
                _logger.info("resumed the last conversation from {run_id}", run_id=resumed)
            session_log.record(Booted(resumed_run_id=resumed))
            session.session_id = live.session_id if live and resumed else None
            # The projection, before the session pool that narrates into it: a settled turn writes
            # through here, and a session from an earlier process finds its home here too.
            projected = await container.get(SessionProjection)
            await projected.ensure_schema()
            project_registry.boot(CHECKOUT)
            # The same sweep `sessions rebuild` runs: every registered project's own session log, upserted by
            # session. Without it the projection only grows as turns settle, and a session already on disk
            # when this process started would never appear.
            await projected.sweep_projects(CHECKOUT, session_log=session_log, store=transcript)
            topics.publish("sessions")
            pool = await container.get(SessionPool)
            scheduler = await container.get(Scheduler)
            # Warmed before the setup is read, so a setup naming an OpenRouter id is on the list by
            # the time the tier store checks it.
            tiers = await container.get(ModelTiers)
            await (await container.get(ModelList)).warm()
            _apply_setup(project_config(CHECKOUT).setup, tiers, custom)
            run = await container.get(RunSettings)
            await serve_durable_runs(stack, session.registry, await ensure_run_engine(stack, state, run), run)
            # Before any loop starts, so a due schedule cannot launch into a session whose old run log is about to be relaunched.
            restore_waiting_run(session, records, topics)
            await restore_waiting_runs(
                pool,
                run_log_folders(CHECKOUT, session.state, [Path(project.root_path) for project in project_registry.registered_projects()]),
                records,
                topics,
            )
            # Model waits, rules and timers each have an independent loop over the same durable jobs.
            stopping = asyncio.Event()
            inspecting = Inspecting(
                jobs=await container.get(JobsAndQueries),
                inspect_run=await container.get(InspectRunHandler),
                attribute_lead=await container.get(AttributeLeadHandler),
                scan_run=await container.get(ScanRunHandler),
                dispatch=await container.get(Dispatch),
                sweep=await container.get(Sweep),
                settings=await container.get(InspectionSettings),
            )
            driving = Driving(wake=await container.get(Wake), name=f"harness:{os.getpid()}")
            timers = coordination_worker(pool, scheduler, await container.get(RunCollaborators), driving, inspecting)
            names = ("timers", INSPECT_RUN_KIND, ATTRIBUTE_LEAD_KIND, SCAN_RUN_KIND, "reconciliation")
            workers = (timers, *inspection_workers(inspecting, driving), reconciliation_worker(inspecting, driving))
            for name, worker in zip(names, workers, strict=True):
                loop_task = asyncio.create_task(worker.run_forever(stop=stopping), name=f"{driving.name}:{name}")
                loop_task.add_done_callback(_report_loop_exit)
                stack.push_async_callback(_stop, loop_task)
            # Registered last so it unwinds first: every loop is asked to stop before it is cancelled.
            stack.callback(stopping.set)
            yield

    return lifespan


def create_app(settings: HarnessSettings | None = None) -> FastAPI:
    """Compose one application: its container, its lifespan, its middleware and its controllers.

    The one place this app's state is created. Calling it twice yields two independent application
    and container pairs, so a test builds its own instead of reaching for a global.

    Args:
        settings: What this deployment was built with. Defaults to reading the environment and the
            committed defaults file.

    Returns:
        The application uvicorn serves.
    """
    built_from = settings if settings is not None else HarnessSettings()
    container = create_container(built_from)
    application = FastAPI(title="harness", lifespan=make_lifespan(container))
    application.state.resolver = ContainerResolver(container)
    # Both spellings of one machine are listed, so AI Factory Studio works whichever URL somebody types.
    # The allowlist stays exact origins, never a pattern, because this API changes real state.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(AI_FACTORY_STUDIO_ORIGINS),
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Content-Type"],
        # AI Factory Studio sends `credentials: "include"`, and a browser discards a credentialed
        # response the server does not allow. This is safe only while the origins above are exact.
        allow_credentials=True,
    )
    # The only `Host` names answered. Without this check, a page on a rebound hostname can call the API from a browser.
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    application.add_exception_handler(session_state.MissingProjectRootError, project_root_is_gone)
    application.add_exception_handler(Exception, any_failure_is_still_json)
    application.add_api_route("/api/events", change_events, methods=["GET"])
    logfire.instrument_fastapi(application, excluded_urls=",".join(POLLED))
    wireup_fastapi.setup(container, application, class_based_handlers=CONTROLLERS)
    return application


if __name__ == "__main__":
    uvicorn.run(app="harness.main:create_app", factory=True, host="127.0.0.1", port=8770)
