"""The scheduler: creates, changes and deletes schedules, and fires the due ones on each tick.

A fire is held rather than launched when the schedule's last run is still live, when its shared
capacity is full, or when the moment falls inside its blackout.
A held fire on capacity always retries on the next tick.
A fire held by overlap or blackout skips to the next turn of the cadence, unless `catch_up` is set or the cadence waits for an event, which has no next turn and keeps its arm.
With `catch_up`, the first fire after the hold clears runs at once and is stamped "caught up".
A schedule whose cadence waits for an event rests after it fires, until it is armed again.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from core.ai.workflows.contracts import LibraryError, unsatisfied_parameters, validate
from core.observability import get_logger
from pydantic import ValidationError

from harness.domain.contracts import AdmissionRefused, SessionRenamed, admitted_arguments, presentation_of, selected_knowledge_bases
from harness.features.running import contracts as running
from harness.features.scheduling.domain import (
    AfterRunCadence,
    Armed,
    BlackoutWindow,
    Cadence,
    OnIndexCadence,
    OnRefreshCadence,
    Schedule,
    ScheduleUpdate,
    blacked_out_until,
    chains_after,
    gate_wait_seconds,
    next_after,
    parse_blackout,
    parse_cadence,
)
from harness.features.scheduling.exceptions import (
    ScheduleNotFoundError,
    ScheduleSettingInvalidError,
    WorkflowParametersMissingError,
    WorkflowUnavailableError,
)

if TYPE_CHECKING:
    from uuid import UUID

    from core.ai.workflows.contracts import RunEvidence, StepRegistry, Workflow, WorkflowArguments, WorkflowLibrary
    from core.cqrs.contracts import TopicSink

    from harness.domain.contracts import GateMode, KnowledgeBaseNames
    from harness.features.scheduling.internal_contracts import ScheduleQueries, Schedules
    from harness.features.sessions.contracts import Sessions
    from harness.session import PooledSessions, Runtime

_logger = get_logger(__name__)

MISSED_OUTCOME = "missed (previous run still live)"
"""Stamped when the schedule's last fire is still running. A clock cadence skips the fire unless catch up is on, and a cadence that waits for an event keeps it."""

CAUGHT_UP_OUTCOME = "caught up"
"""Stamped when `catch_up` fires a schedule as soon as its overlap or blackout hold clears."""

ENGINE_UNREACHABLE_OUTCOME = "engine unreachable"
"""Stamped when the run engine this deployment is configured for answered nothing."""

ORCHESTRATE_ON_TEMPORAL = (
    "this deployment runs on Temporal, where an orchestrated gate has no counterpart yet. Save the schedule in wait or auto mode."
)
"""The refusal a schedule saved with gate mode `orchestrate` gets on a deployment that runs on Temporal."""

_BLACKOUT_WALK_LIMIT = 1000
"""The most cadence occurrences `_next_outside_blackout` walks past before it gives up."""


def live_phase(pool: Sessions, session_id: str) -> str | None:
    """Where a session's run stands right now.

    It reads only the sessions this process already holds and never opens one.

    Args:
        pool: The sessions this process holds.
        session_id: The runner session's id. Empty for a schedule that never fired.

    Returns:
        The run's phase, or `None` when the session holds no live run or this process does not hold it.
    """
    if not session_id:
        return None
    session = next((held for held in pool.held_sessions() if held.session_id == session_id), None)
    return None if session is None else running.run_phase(session)


async def _runner_session(pool: Sessions, schedule: Schedule) -> Runtime:
    """The runner session every fire of this schedule shares, opened fresh when the pool no longer has it."""
    if schedule.session_id:
        try:
            return await pool.get(schedule.session_id)
        except KeyError:
            pass
    session = await pool.create(None, session_kind="runner")
    session.session_log.record(SessionRenamed(session_id=session.session_id or "", title=schedule.workflow_id))
    return session


async def current_version(library: WorkflowLibrary, workflow_id: str) -> str:
    """The newest live version of a workflow, the version a schedule fires.

    Args:
        library: The workflow library.
        workflow_id: The workflow id the schedule holds.

    Returns:
        The newest live version, or `workflow_id` itself when the library cannot answer.
    """
    current = getattr(library, "current", None)
    if current is None:
        return workflow_id
    try:
        return await current(workflow_id)
    except LibraryError:
        return workflow_id


async def _launch(session: Runtime, schedule: Schedule, run: running.RunCollaborators) -> str:
    """Validate the newest version of the schedule's workflow in the session's environment and launch it, or say why not."""
    workflow_id = await current_version(session.library, schedule.workflow_id)
    try:
        spec = await _checked_workflow(session.library, session.registry, workflow_id)
        _require_parameters(workflow_id, spec, schedule.arguments)
    except WorkflowUnavailableError as exc:
        return f"refused: {exc.reason}"
    except (running.WorkflowNotRunnableError, WorkflowParametersMissingError) as exc:
        return f"refused: {exc}"
    try:
        params = await admitted_arguments(
            spec,
            schedule.arguments,
            presentation=await presentation_of(session.library, workflow_id),
            names=run.knowledge_bases,
        )
    except AdmissionRefused as exc:
        return f"refused: {exc}"
    _logger.info(
        "launching {workflow} from schedule {schedule}: gate_mode={mode}",
        workflow=workflow_id,
        schedule=str(schedule.id),
        mode=schedule.gate_mode,
    )
    try:
        await running.start(
            session,
            records=run.records,
            topics=run.topics,
            spec=spec,
            workflow_id=workflow_id,
            params=params,
            run=run.run_settings,
            gate_timeout=gate_wait_seconds(schedule.gate_policy),
            runner=running.schedule_runner(str(schedule.id)),
            gate_mode=schedule.gate_mode,
        )
    except running.RunEngineUnavailableError as exc:
        _logger.warning("schedule {schedule} found no engine: {reason}", schedule=str(schedule.id), reason=str(exc))
        return ENGINE_UNREACHABLE_OUTCOME
    except running.AutomodeUnavailableError:
        return f"refused: {ORCHESTRATE_ON_TEMPORAL}"
    return "launched"


def _refuse_orchestrate_on_temporal(gate_mode: GateMode | None, run_engine: Literal["in_process", "temporal"]) -> None:
    """Refuse gate mode `orchestrate` on a deployment that runs on Temporal."""
    if gate_mode == "orchestrate" and run_engine == "temporal":
        raise ScheduleSettingInvalidError(ORCHESTRATE_ON_TEMPORAL)


def _held_before(last_outcome: str) -> bool:
    """Whether the last outcome names a hold that a successful fire now catches up from."""
    return last_outcome == MISSED_OUTCOME or last_outcome.startswith("blacked out until ")


def _next_outside_blackout(cadence: Cadence, windows: tuple[BlackoutWindow, ...], moment: datetime) -> datetime | None:
    """The next cadence occurrence after `moment` that lands outside every window, `None` for a cadence that waits for an event."""
    candidate = next_after(cadence, moment)
    for _ in range(_BLACKOUT_WALK_LIMIT):
        if candidate is None or blacked_out_until(windows, candidate) is None:
            return candidate
        candidate = next_after(cadence, candidate)
    return candidate


def _after_itself(workflow_name: str) -> str:
    """The refusal for an after-run cadence naming the schedule's own workflow."""
    return f"a schedule of {workflow_name!r} cannot fire after {workflow_name!r}, because every run that succeeds would fire it again"


async def _refuse_firing_after_itself(
    library: WorkflowLibrary, registry: StepRegistry, held: Schedule, changes: ScheduleUpdate, cadence: Cadence | None
) -> None:
    """Refuse a change that leaves an after-run cadence naming the workflow the schedule runs."""
    if changes.cadence is None and changes.workflow_id is None:
        return
    try:
        effective = cadence if cadence is not None else parse_cadence(held.cadence)
    except ValueError:
        return
    if not isinstance(effective, AfterRunCadence):
        return
    spec = await _checked_workflow(library, registry, changes.workflow_id or held.workflow_id)
    if effective.workflow_name == spec.name:
        raise ScheduleSettingInvalidError(_after_itself(spec.name))


async def _checked_workflow(library: WorkflowLibrary, registry: StepRegistry, workflow_id: str) -> Workflow:
    """The workflow's definition when it validates against `registry`, or the error that says why not."""
    try:
        spec = await library.fetch_workflow(workflow_id)
    except (LibraryError, ValidationError) as exc:
        raise WorkflowUnavailableError(workflow_id, reason=str(exc)) from exc
    report = validate(spec, registry)
    if not report.ok:
        raise running.WorkflowNotRunnableError(workflow_id, issues=report.issues)
    return spec


def _require_parameters(workflow_id: str, spec: Workflow, arguments: WorkflowArguments) -> None:
    """Refuse arguments that leave a required parameter of the workflow unfilled."""
    missing = unsatisfied_parameters(spec, arguments)
    if missing:
        raise WorkflowParametersMissingError(workflow_id, missing=missing)


async def _admitted(
    library: WorkflowLibrary, workflow_id: str, spec: Workflow, arguments: WorkflowArguments, names: KnowledgeBaseNames | None
) -> WorkflowArguments:
    """The arguments as the schedule will fire with them, admitted against the workflow's parameters now."""
    return await admitted_arguments(spec, arguments, presentation=await presentation_of(library, workflow_id), names=names)


def _restored_next_fire(held: Schedule, changes: ScheduleUpdate, now: datetime) -> datetime | None:
    """The next fire a save gives a schedule that has none, or `None` to leave it."""
    if held.next_run_at is not None:
        return None
    return next_after(parse_cadence(changes.cadence if changes.cadence is not None else held.cadence), now)


def _cadence_or_none(text: str) -> Cadence | None:
    """The parsed cadence, or `None` for stored text that no longer parses."""
    try:
        return parse_cadence(text)
    except ValueError:
        return None


async def _selected_by(library: WorkflowLibrary, schedule: Schedule) -> frozenset[str]:
    """The knowledge bases a schedule's arguments select, read off the card of the version it fires."""
    workflow_id = await current_version(library, schedule.workflow_id)
    return selected_knowledge_bases(await presentation_of(library, workflow_id), schedule.arguments)


class Scheduler:
    """Creates, changes and deletes schedules, and fires the due ones on each tick.

    Every change to a schedule is announced on the `schedules` topic. A refusal announces nothing.
    """

    def __init__(self, schedules: Schedules, queries: ScheduleQueries, topics: TopicSink) -> None:
        """Hold the stored schedules, their reads and the topic changes are announced on.

        Args:
            schedules: The stored schedules, written one targeted change at a time.
            queries: The reads over the stored schedules.
            topics: Where every change to a schedule is announced.
        """
        self._schedules = schedules
        self._queries = queries
        self._topics = topics

    async def arm_after_refresh(self, library: WorkflowLibrary, *, event_id: UUID, knowledge_base: str, now: datetime) -> Armed:
        """Arm every enabled on-refresh schedule whose arguments select `knowledge_base`, once per refresh run.

        The event id is recorded with the arming, so the same refresh run reported again arms nothing.
        A schedule whose stored cadence no longer parses is skipped.

        Args:
            library: The workflow library each schedule's card is read from.
            event_id: The studio's id for the refresh run's ending.
            knowledge_base: The registry name of the refreshed knowledge base.
            now: The next fire each armed schedule gets.

        Returns:
            Whether the refresh run was reported before, and which schedules will fire on the next tick.

        Raises:
            CardUnreadableError: The library could not answer for a schedule's workflow card. Nothing is recorded then.
        """
        chosen: list[UUID] = []
        for schedule in await self._queries.list_all():
            if not schedule.enabled or not isinstance(_cadence_or_none(schedule.cadence), OnRefreshCadence):
                continue
            if knowledge_base in await _selected_by(library, schedule):
                chosen.append(schedule.id)
        armed = await self._schedules.arm(chosen, at=now, event_id=event_id)
        if armed.schedule_ids:
            self._topics.publish("schedules")
        return armed

    async def arm_after_run(self, library: WorkflowLibrary, evidence: RunEvidence, *, now: datetime) -> tuple[UUID, ...]:
        """Arm every enabled after-run schedule waiting for the ended run's workflow over a knowledge base the run also selected.

        A run whose checked outcome is not succeeded or partial arms nothing, and neither does a run of a workflow the
        library no longer serves, such as a draft.

        Args:
            library: The workflow library the ended run's workflow and every card are read from.
            evidence: What the run's ending recorded.
            now: The next fire each armed schedule gets.

        Returns:
            The schedules that will fire on the next tick.

        Raises:
            CardUnreadableError: The library could not answer for a card.
        """
        if not chains_after(evidence):
            return ()
        waiting: list[tuple[Schedule, AfterRunCadence]] = []
        for schedule in await self._queries.list_all():
            cadence = _cadence_or_none(schedule.cadence)
            if schedule.enabled and isinstance(cadence, AfterRunCadence):
                waiting.append((schedule, cadence))
        if not waiting:
            return ()
        try:
            name = (await library.fetch_workflow(evidence.workflow_id)).name
        except (LibraryError, ValidationError):
            return ()
        ran_over = selected_knowledge_bases(await presentation_of(library, evidence.workflow_id), evidence.params)
        chosen: list[UUID] = []
        for schedule, cadence in waiting:
            if cadence.workflow_name == name and ran_over & await _selected_by(library, schedule):
                chosen.append(schedule.id)
        if not chosen:
            return ()
        armed = await self._schedules.arm(chosen, at=now)
        if armed.schedule_ids:
            self._topics.publish("schedules")
        return armed.schedule_ids

    async def schedules(self) -> list[Schedule]:
        """Every schedule.

        Returns:
            The schedules, in the order the read port gives them.
        """
        return await self._queries.list_all()

    async def create_schedule(  # noqa: PLR0913 (a schedule's settings are one flat contract)
        self,
        library: WorkflowLibrary,
        registry: StepRegistry,
        *,
        workflow_id: str,
        arguments: WorkflowArguments,
        cadence: str,
        gate_policy: str,
        gate_mode: GateMode,
        capacity_key: str | None,
        capacity_limit: int | None,
        blackout: str | None,
        catch_up: bool,
        names: KnowledgeBaseNames | None,
        now: datetime,
        run_engine: Literal["in_process", "temporal"],
    ) -> Schedule:
        """Add a schedule after checking its workflow, its arguments and every setting.

        The checks run in this order: the workflow, its required parameters, its arguments, the cadence, the gate policy, the
        blackout, the gate mode.

        Args:
            library: The workflow library the workflow is read from.
            registry: The steps this process can run, which the workflow must validate against.
            workflow_id: The workflow the schedule runs.
            arguments: One value per declared parameter of the workflow.
            cadence: How often the schedule fires.
            gate_policy: `wait`, or `fail_after:<seconds>`.
            gate_mode: `wait`, `orchestrate` or `auto`.
            capacity_key: A name shared with other schedules to cap how many run at once.
            capacity_limit: How many schedules sharing `capacity_key` may run at once.
            blackout: Daily quiet hours, or `None` for none.
            catch_up: Whether a held fire runs once as soon as the hold clears.
            names: The registered knowledge bases, for a workflow whose parameters select one.
            now: The moment the first fire is counted from.
            run_engine: The engine this deployment runs workflows on. Temporal refuses gate mode `orchestrate`.

        Returns:
            The stored schedule.

        Raises:
            WorkflowUnavailableError: The library cannot serve the workflow.
            WorkflowNotRunnableError: The workflow does not validate against `registry`.
            WorkflowParametersMissingError: The arguments leave a required parameter without a value.
            AdmissionRefused: An argument does not fit its parameter.
            ScheduleSettingInvalidError: The cadence, gate policy or blackout is not one of its shapes, an after-run cadence names the
                schedule's own workflow, or the gate mode is `orchestrate` on Temporal.
        """
        spec = await _checked_workflow(library, registry, workflow_id)
        _require_parameters(workflow_id, spec, arguments)
        admitted = await _admitted(library, workflow_id, spec, arguments, names)
        try:
            parsed_cadence = parse_cadence(cadence)
            gate_wait_seconds(gate_policy)
            if blackout is not None:
                parse_blackout(blackout)
        except ValueError as exc:
            raise ScheduleSettingInvalidError(str(exc)) from exc
        if isinstance(parsed_cadence, AfterRunCadence) and parsed_cadence.workflow_name == spec.name:
            raise ScheduleSettingInvalidError(_after_itself(spec.name))
        _refuse_orchestrate_on_temporal(gate_mode, run_engine)
        schedule = Schedule(
            workflow_id=workflow_id,
            arguments=admitted,
            cadence=cadence,
            gate_policy=gate_policy,
            gate_mode=gate_mode,
            capacity_key=capacity_key,
            capacity_limit=capacity_limit,
            blackout=blackout,
            catch_up=catch_up,
            next_run_at=next_after(parsed_cadence, now),
        )
        await self._schedules.insert(schedule)
        self._topics.publish("schedules")
        return schedule

    async def update_schedule(  # noqa: PLR0913 (a change reads the session it runs in and its own clock)
        self,
        library: WorkflowLibrary,
        registry: StepRegistry,
        schedule_id: UUID,
        changes: ScheduleUpdate,
        *,
        names: KnowledgeBaseNames | None,
        run_engine: Literal["in_process", "temporal"],
        now: datetime | None = None,
    ) -> Schedule:
        """Apply the set fields of `changes` to one schedule, checking each one first.

        A change of workflow or arguments checks the arguments against the workflow the schedule will point at.
        The arguments are written only when `changes` sets them.
        A schedule with no next fire, such as one whose fire failed, gets its next fire from its cadence again.
        A change that sets a cadence waiting for an event clears the next fire, so the schedule rests until something arms it.
        Turning the schedule off stamps it paused at `now`, and turning it on clears the stamp.

        Args:
            library: The workflow library the workflow is read from.
            registry: The steps this process can run, which the workflow must validate against.
            schedule_id: The schedule's id.
            changes: The fields to change, as the caller sent them.
            names: The registered knowledge bases, for a workflow whose parameters select one.
            run_engine: The engine this deployment runs workflows on. Temporal refuses a change to gate mode `orchestrate`.
            now: The moment a restored next fire is counted from and a pause is stamped with. The current time when not given.

        Returns:
            The schedule as stored after the change.

        Raises:
            ScheduleNotFoundError: No schedule has that id.
            WorkflowUnavailableError: The library cannot serve the workflow.
            WorkflowNotRunnableError: The workflow does not validate against `registry`.
            WorkflowParametersMissingError: The arguments leave a required parameter without a value.
            AdmissionRefused: An argument does not fit its parameter.
            ScheduleSettingInvalidError: The cadence, gate policy or blackout is not one of its shapes, an after-run cadence names the
                schedule's own workflow, or the change sets gate mode `orchestrate` on Temporal.
        """
        held = await self._schedules.get(schedule_id)
        if held is None:
            raise ScheduleNotFoundError(schedule_id)
        patched = await self._patched_arguments(library, registry, held, changes, names=names)
        try:
            cadence = parse_cadence(changes.cadence) if changes.cadence is not None else None
            if changes.gate_policy is not None:
                gate_wait_seconds(changes.gate_policy)
            if changes.blackout is not None:
                parse_blackout(changes.blackout)
        except ValueError as exc:
            raise ScheduleSettingInvalidError(str(exc)) from exc
        _refuse_orchestrate_on_temporal(changes.gate_mode, run_engine)
        await _refuse_firing_after_itself(library, registry, held, changes, cadence)
        rests = isinstance(cadence, OnRefreshCadence | AfterRunCadence | OnIndexCadence)
        moment = now if now is not None else datetime.now(UTC)
        resolved = changes.model_copy(
            update={
                "arguments": patched,
                "next_run_at": None if rests else _restored_next_fire(held, changes, moment),
                "clears_next_fire": rests,
                "paused_at": held.paused_at_after(enabled=changes.enabled, now=moment),
            }
        )
        if not await self._schedules.update(schedule_id, resolved):
            raise ScheduleNotFoundError(schedule_id)
        schedule = await self._schedules.get(schedule_id)
        if schedule is None:  # pragma: no cover (deleted between the update and this read)
            raise ScheduleNotFoundError(schedule_id)
        self._topics.publish("schedules")
        return schedule

    async def _patched_arguments(
        self, library: WorkflowLibrary, registry: StepRegistry, held: Schedule, changes: ScheduleUpdate, *, names: KnowledgeBaseNames | None
    ) -> WorkflowArguments | None:
        """The arguments to write, or `None` to leave them, checked against the workflow the schedule will name."""
        if changes.workflow_id is None and changes.arguments is None:
            return None
        named = changes.workflow_id if changes.workflow_id is not None else held.workflow_id
        spec = await _checked_workflow(library, registry, named)
        arguments = changes.arguments if changes.arguments is not None else held.arguments
        _require_parameters(named, spec, arguments)
        admitted = await _admitted(library, named, spec, arguments, names)
        return admitted if changes.arguments is not None else None

    async def delete_schedule(self, schedule_id: UUID) -> None:
        """Remove one schedule. A run it already started keeps going.

        Args:
            schedule_id: The schedule's id.

        Raises:
            ScheduleNotFoundError: No schedule has that id.
        """
        if not await self._schedules.delete(schedule_id):
            raise ScheduleNotFoundError(schedule_id)
        self._topics.publish("schedules")

    async def run_now(self, pool: Sessions, schedule_id: UUID, *, run: running.RunCollaborators, now: datetime | None = None) -> str:
        """Fire one schedule immediately, through the same path the tick uses.

        Args:
            pool: The sessions the fire's runner session comes from.
            schedule_id: The schedule's id.
            run: Where the fired run's records land and which engine runs it.
            now: The moment of the fire. The current time when not given.

        Returns:
            What the fire did, in words.

        Raises:
            ScheduleNotFoundError: No schedule has that id.
        """
        schedule = await self._schedules.get(schedule_id)
        if schedule is None:
            raise ScheduleNotFoundError(schedule_id)
        return await self._fire(pool, schedule, run=run, moment=now if now is not None else datetime.now(UTC))

    async def tick(self, pool: Sessions, *, run: running.RunCollaborators, now: datetime | None = None) -> None:
        """Fire every due schedule once, or stamp why it was held.

        A schedule that raises costs only its own turn. It is stamped `failed, <reason>` with no next fire,
        so it stops coming due until an owner saves it again.

        Args:
            pool: The sessions each fire's runner session comes from.
            run: Where the fired runs' records land and which engine runs them.
            now: The moment of the tick. The current time when not given.
        """
        moment = now if now is not None else datetime.now(UTC)
        for schedule in await self._schedules.due(moment):
            try:
                await self._fire(pool, schedule, run=run, moment=moment)
            except Exception as exc:
                _logger.exception("schedule {schedule} failed to fire", schedule=str(schedule.id))
                with suppress(Exception):  # a failing stamp must not end the tick either
                    await self._schedules.stamp(
                        schedule.id, last_run_at=moment, last_outcome=f"failed, {exc}", next_run_at=None, session_id=schedule.session_id
                    )
                self._topics.publish("schedules")

    async def _capacity_live_count(self, pool: Sessions, capacity_key: str) -> int:
        """How many schedules sharing `capacity_key` have a live run, read without opening a session."""
        siblings = [row for row in await self._queries.list_all() if row.capacity_key == capacity_key]
        return sum(1 for row in siblings if live_phase(pool, row.session_id) is not None)

    async def _fire(self, pool: Sessions, schedule: Schedule, *, run: running.RunCollaborators, moment: datetime) -> str:
        """Fire one schedule, or record the first hold among overlap, capacity and blackout, then stamp the tick log."""
        session = await _runner_session(pool, schedule)
        cadence = parse_cadence(schedule.cadence)
        windows = parse_blackout(schedule.blackout) if schedule.blackout else ()
        keeps_held_fire = schedule.catch_up or isinstance(cadence, OnRefreshCadence | AfterRunCadence | OnIndexCadence)

        if running.run_phase(session) is not None:
            outcome = MISSED_OUTCOME
            next_run_at = schedule.next_run_at if keeps_held_fire else next_after(cadence, moment)
        elif (
            schedule.capacity_key is not None
            and schedule.capacity_limit is not None
            and await self._capacity_live_count(pool, schedule.capacity_key) >= schedule.capacity_limit
        ):
            outcome = f"held, capacity {schedule.capacity_key} is full"
            next_run_at = schedule.next_run_at
        elif windows and (window_end := blacked_out_until(windows, moment)) is not None:
            outcome = f"blacked out until {window_end.strftime('%H:%M')}"
            next_run_at = schedule.next_run_at if keeps_held_fire else _next_outside_blackout(cadence, windows, moment)
        else:
            launched = await _launch(session, schedule, run)
            outcome = CAUGHT_UP_OUTCOME if schedule.catch_up and launched == "launched" and _held_before(schedule.last_outcome) else launched
            next_run_at = next_after(cadence, moment)

        await self._schedules.stamp(
            schedule.id, last_run_at=moment, last_outcome=outcome, next_run_at=next_run_at, session_id=session.session_id or ""
        )
        self._topics.publish("schedules")
        return outcome


class RunEndArming:
    """Arms the schedules waiting for a workflow when a run of it ends, reading the library of this process's own session.

    The pool is asked for only when a run ends that can arm something, never when this is built.
    """

    def __init__(self, scheduler: Scheduler, sessions: PooledSessions) -> None:
        """Hold the scheduler and where this process's sessions are found.

        Args:
            scheduler: The scheduler the after-run schedules are armed through.
            sessions: The sessions this process holds, whose own session's library names a run's workflow.
        """
        self._scheduler = scheduler
        self._sessions = sessions

    async def run_ended(self, evidence: RunEvidence) -> None:
        """Arm every after-run schedule this run's ending satisfies.

        Args:
            evidence: What the run's ending recorded.
        """
        if not chains_after(evidence):
            return
        library = (await self._sessions.pool()).default.library
        await self._scheduler.arm_after_run(library, evidence, now=datetime.now(UTC))
