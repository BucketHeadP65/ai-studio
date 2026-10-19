"""The schedule routes: list, create, change and delete schedules, preview a cadence, fire one now, and hear about a refresh run.

Every route checks a workflow against this process's own default session.
"""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import JsonValue
from wireup import Injected
from wireup.integration.fastapi import WireupRoute

from harness.adapters.contracts import Notifier
from harness.domain.contracts import AdmissionRefused, CardUnreadableError
from harness.features.running.contracts import RunCollaborators, WorkflowNotRunnableError, schedule_spend
from harness.features.scheduling.api_models import CadenceIn, RefreshRunIn, RefreshRunOut, ScheduleIn, ScheduleOut, SchedulePatch
from harness.features.scheduling.domain import (
    NEW_SOURCES_TITLE,
    UNKNOWN_SCHEDULE,
    Schedule,
    ScheduleUpdate,
    new_sources_notice,
    parse_cadence,
    preview_fires,
)
from harness.features.scheduling.exceptions import (
    ScheduleNotFoundError,
    ScheduleSettingInvalidError,
    WorkflowParametersMissingError,
    WorkflowUnavailableError,
)
from harness.features.scheduling.service import Scheduler, current_version, live_phase
from harness.features.sessions.contracts import Sessions


def _no_such_schedule() -> JSONResponse:
    """The refusal every schedule route gives for an id no schedule has."""
    return JSONResponse({"ok": False, "message": UNKNOWN_SCHEDULE}, status_code=404)


def _refusal(exc: Exception) -> JSONResponse:
    """The JSON refusal a scheduler error answers with."""
    match exc:
        case WorkflowUnavailableError():
            return JSONResponse({"ok": False, "message": exc.reason}, status_code=404)
        case ScheduleNotFoundError():
            return _no_such_schedule()
        case CardUnreadableError():
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=503)
        case _:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


class SchedulingController:
    """The schedule routes, under `/api/schedules`."""

    router = APIRouter(prefix="/api/schedules", tags=["scheduling"], route_class=WireupRoute)

    def __init__(self, scheduler: Scheduler, sessions: Sessions, run: RunCollaborators) -> None:
        """Hold the scheduler, the sessions a fire's runner session comes from, and what a fired run reaches.

        Args:
            scheduler: The scheduler every route goes through.
            sessions: The sessions this process holds.
            run: Where a fired run's records land and which engine runs it.
        """
        self._scheduler = scheduler
        self._sessions = sessions
        self._run = run

    async def _out(self, schedule: Schedule) -> dict[str, JsonValue]:
        """The schedule as a listing row, naming the newest live version of its workflow."""
        live = live_phase(self._sessions, schedule.session_id)
        spend = await schedule_spend(self._run.records.spend, schedule.workflow_id, str(schedule.id), now=datetime.now(UTC))
        workflow_id = await current_version(self._sessions.default.library, schedule.workflow_id)
        return ScheduleOut(
            **{**schedule.model_dump(), "workflow_id": workflow_id},
            live=live,
            spend_today_usd=spend.today,
            spend_week_usd=spend.week,
            spend_today_unpriced=spend.today_unpriced,
            spend_week_unpriced=spend.week_unpriced,
        ).model_dump(mode="json")

    @router.get("")
    async def list_schedules(self) -> JSONResponse:
        """Every schedule, its next fire, its last outcome, and whether it is running right now."""
        schedules = await self._scheduler.schedules()
        rows = await asyncio.gather(*(self._out(schedule) for schedule in schedules))
        return JSONResponse({"schedules": list(rows)})

    @router.post("")
    async def create_schedule(self, body: ScheduleIn) -> JSONResponse:
        """Add a schedule for `body.workflow_id`, refused when it does not check out here."""
        session = self._sessions.default
        try:
            schedule = await self._scheduler.create_schedule(
                session.library,
                session.registry,
                workflow_id=body.workflow_id,
                arguments=body.arguments,
                cadence=body.cadence,
                gate_policy=body.gate_policy,
                gate_mode=body.gate_mode,
                capacity_key=body.capacity_key,
                capacity_limit=body.capacity_limit,
                blackout=body.blackout,
                catch_up=body.catch_up,
                names=self._run.knowledge_bases,
                now=datetime.now(UTC),
                run_engine=self._run.run_settings.run_engine,
            )
        except (WorkflowUnavailableError, WorkflowNotRunnableError, WorkflowParametersMissingError, ScheduleSettingInvalidError) as exc:
            return _refusal(exc)
        except AdmissionRefused as exc:
            return _refusal(exc)
        return JSONResponse({"schedule": await self._out(schedule)}, status_code=201)

    @router.patch("/{schedule_id}")
    async def patch_schedule(self, schedule_id: UUID, body: SchedulePatch) -> JSONResponse:
        """Apply the fields `body` sets to one schedule, checking again a workflow or arguments it changes."""
        session = self._sessions.default
        try:
            schedule = await self._scheduler.update_schedule(
                session.library,
                session.registry,
                schedule_id,
                ScheduleUpdate(**body.model_dump()),
                names=self._run.knowledge_bases,
                run_engine=self._run.run_settings.run_engine,
            )
        except (ScheduleNotFoundError, WorkflowUnavailableError, WorkflowNotRunnableError, WorkflowParametersMissingError, ScheduleSettingInvalidError) as exc:
            return _refusal(exc)
        except AdmissionRefused as exc:
            return _refusal(exc)
        return JSONResponse({"schedule": await self._out(schedule)})

    @router.delete("/{schedule_id}")
    async def delete_schedule(self, schedule_id: UUID) -> JSONResponse:
        """Remove one schedule. A run it already started is left alone."""
        try:
            await self._scheduler.delete_schedule(schedule_id)
        except ScheduleNotFoundError:
            return _no_such_schedule()
        return JSONResponse({"ok": True})

    @router.post("/{schedule_id}/run-now")
    async def run_schedule_now(self, schedule_id: UUID) -> JSONResponse:
        """Fire one schedule immediately, through the same path the tick uses."""
        try:
            outcome = await self._scheduler.run_now(self._sessions, schedule_id, run=self._run)
        except ScheduleNotFoundError:
            return _no_such_schedule()
        return JSONResponse({"ok": True, "outcome": outcome})

    @router.post("/preview")
    async def preview_cadence(self, body: CadenceIn) -> JSONResponse:
        """The next three fires `body.cadence` would produce, counted from now. A cadence that waits for an event lists none."""
        try:
            cadence = parse_cadence(body.cadence)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        occurrences = [moment.isoformat() for moment in preview_fires(cadence)]
        return JSONResponse({"next": occurrences})

    @router.post("/refresh-runs")
    async def report_refresh_run(self, body: RefreshRunIn, notifier: Injected[Notifier]) -> JSONResponse:
        """Hear that a knowledge base's refresh run ended, arm every on-refresh schedule that selects it and tell the owner, once per run."""
        if body.sources_created == 0:
            return JSONResponse(RefreshRunOut().model_dump(mode="json"))
        try:
            armed = await self._scheduler.arm_after_refresh(
                self._sessions.default.library, event_id=body.event_id, knowledge_base=body.knowledge_base, now=datetime.now(UTC)
            )
        except CardUnreadableError as exc:
            return _refusal(exc)
        if armed.repeat:
            return JSONResponse(RefreshRunOut(repeat=True).model_dump(mode="json"))
        notice = new_sources_notice(body.knowledge_base, body.sources_created, body.requested_by_entry)
        notified = await notifier.notify(NEW_SOURCES_TITLE, notice)
        return JSONResponse(RefreshRunOut(notified=notified, armed=list(armed.schedule_ids)).model_dump(mode="json"))
