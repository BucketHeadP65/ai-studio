"""Agent tools for schedules: list them, create one, change one, delete one, fire one now.

Every tool goes through the scheduler, the same path the schedule routes use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic_ai.exceptions import ModelRetry

from harness.domain.contracts import AdmissionRefused
from harness.features.running.contracts import WorkflowNotRunnableError
from harness.features.scheduling.domain import UNKNOWN_SCHEDULE, Schedule, ScheduleUpdate, parse_cadence, preview_fires
from harness.features.scheduling.exceptions import (
    ScheduleNotFoundError,
    ScheduleSettingInvalidError,
    WorkflowParametersMissingError,
    WorkflowUnavailableError,
)

if TYPE_CHECKING:
    from core.ai.workflows.contracts import WorkflowArguments
    from pydantic_ai import RunContext

    from harness.domain.contracts import GateMode, KnowledgeBaseNames
    from harness.features.running.contracts import RunCollaborators
    from harness.features.scheduling.service import Scheduler
    from harness.session import PooledSessions, Runtime


def _parse_id(schedule_id: str) -> UUID:
    """The id as a `UUID`, or the same refusal an unknown id gets."""
    try:
        return UUID(schedule_id)
    except ValueError as exc:
        raise ModelRetry(UNKNOWN_SCHEDULE) from exc


def _describe(schedule: Schedule) -> str:
    """One schedule as a line of words, with its capacity, blackout and catch up when set."""
    next_fire = f"next fire {schedule.next_run_at.isoformat()}" if schedule.next_run_at is not None else "no next fire scheduled"
    outcome = f"last outcome {schedule.last_outcome}" if schedule.last_outcome else "never fired yet"
    bits = [f"workflow {schedule.workflow_id}", f"cadence {schedule.cadence}", "enabled" if schedule.enabled else "disabled"]
    bits += [next_fire, outcome]
    if schedule.capacity_key is not None:
        bits.append(f"capacity {schedule.capacity_key} limit {schedule.capacity_limit}")
    if schedule.blackout is not None:
        bits.append(f"blackout {schedule.blackout}")
    if schedule.catch_up:
        bits.append("catch up on")
    return f"{schedule.id}. " + ", ".join(bits) + "."


async def schedules(scheduler: Scheduler) -> str:
    """Every schedule, one line each: workflow, cadence, enabled, next fire, last outcome.

    Args:
        scheduler: The scheduler that holds the schedules.

    Returns:
        One line per schedule, oldest first, or a sentence saying there are none.
    """
    rows = sorted(await scheduler.schedules(), key=lambda row: row.created_at)
    if not rows:
        return "No schedules exist yet."
    return "\n".join(_describe(row) for row in rows)


async def schedule_workflow(  # noqa: PLR0913 (a schedule's settings are one flat contract, the same one the HTTP route takes)
    scheduler: Scheduler,
    ctx: RunContext[Runtime],
    workflow_id: str,
    cadence: str,
    *,
    arguments: WorkflowArguments | None = None,
    names: KnowledgeBaseNames | None = None,
    gate_policy: str = "wait",
    gate_mode: GateMode = "wait",
    capacity_key: str | None = None,
    capacity_limit: int | None = None,
    blackout: str | None = None,
    catch_up: bool = False,
) -> str:
    """Create a standing schedule that fires `workflow_id` on its own, on `cadence`, from now on.

    Says the schedule's id and its next three fires. Load `orchestrating-a-run` for what each
    setting (`gate_mode`, `capacity_key`, `blackout`, `catch_up`) does.

    Args:
        scheduler: The scheduler the new schedule is filed with.
        ctx: The run context, checked against this session's own registered workflows.
        workflow_id: A library id this session's own environment can run.
        cadence: `every <N><s, m, h, or d>`, a 5 field cron expression, `on refresh` (once after each refresh
            of the knowledge base the arguments select that brought in new sources), or `after <workflow name>`
            (once after a run of that workflow over the same knowledge base ends succeeded or partial).
        arguments: The workflow's parameters, one per declared parameter, each as the JSON value
            that parameter's own type asks for.
        names: Which knowledge bases are registered here, for a workflow whose parameters select one.
        gate_policy: `wait` to hold a gate for a person forever, or `fail_after:<seconds>`.
        gate_mode: `wait`, `orchestrate`, or `auto`. See `orchestrating-a-run`.
        capacity_key: A name shared with other schedules to cap how many of them run at once.
        capacity_limit: How many schedules sharing `capacity_key` may run at once.
        blackout: One or more `HH:MM-HH:MM` daily quiet hours, comma separated.
        catch_up: Fire once immediately when a held fire clears, instead of waiting for the next
            normal turn.

    Returns:
        The new schedule's id and its next three fires, or that it rests until an event arms it.

    Raises:
        ModelRetry: `workflow_id` does not check out here, an argument is not what its parameter
            declares, or `cadence`, `gate_policy`, or `blackout` is not one of its own shapes.
    """
    now = datetime.now(UTC)
    session = ctx.deps
    try:
        schedule = await scheduler.create_schedule(
            session.library,
            session.registry,
            workflow_id=workflow_id,
            arguments=arguments or {},
            cadence=cadence,
            gate_policy=gate_policy,
            gate_mode=gate_mode,
            capacity_key=capacity_key,
            capacity_limit=capacity_limit,
            blackout=blackout,
            catch_up=catch_up,
            names=names,
            now=now,
        )
    except WorkflowUnavailableError as exc:
        msg = f"cannot schedule {workflow_id}. {exc.reason}"
        raise ModelRetry(msg) from exc
    except (WorkflowNotRunnableError, WorkflowParametersMissingError) as exc:
        msg = f"cannot schedule {workflow_id}. {exc}"
        raise ModelRetry(msg) from exc
    except AdmissionRefused as exc:
        msg = f"this schedule cannot hold those values. {exc}"
        raise ModelRetry(msg) from exc
    except ScheduleSettingInvalidError as exc:
        raise ModelRetry(str(exc)) from exc
    fires = preview_fires(parse_cadence(cadence), since=now)
    if not fires:
        return f"Scheduled {workflow_id} as {schedule.id}, cadence {cadence}. It rests until a refresh run or a workflow run arms it, never by the clock."
    when = ", ".join(moment.isoformat() for moment in fires)
    return f"Scheduled {workflow_id} as {schedule.id}, cadence {cadence}. Next three fires. {when}."


async def update_schedule(  # noqa: PLR0913 (mirrors the HTTP patch route's flat contract)
    scheduler: Scheduler,
    ctx: RunContext[Runtime],
    schedule_id: str,
    *,
    workflow_id: str | None = None,
    cadence: str | None = None,
    arguments: WorkflowArguments | None = None,
    names: KnowledgeBaseNames | None = None,
    enabled: bool | None = None,
    gate_policy: str | None = None,
    gate_mode: GateMode | None = None,
    capacity_key: str | None = None,
    capacity_limit: int | None = None,
    blackout: str | None = None,
    catch_up: bool | None = None,
) -> str:
    """Change one or more of a schedule's own settings. An argument left out leaves its setting alone.

    Says the schedule back in full afterward.

    Args:
        scheduler: The scheduler that holds the schedule.
        ctx: The run context, checked against this session's own registered workflows when
            `workflow_id` is given.
        schedule_id: The schedule's own id, as shown by `schedules`.
        workflow_id: Point the schedule at a different saved workflow.
        cadence: Change how it fires, in any form `schedule_workflow` takes.
        arguments: Replace the workflow's parameters, each as the JSON value that parameter's own
            type asks for.
        names: Which knowledge bases are registered here, for a workflow whose parameters select one.
        enabled: Pause it (false) or resume it (true).
        gate_policy: `wait` or `fail_after:<seconds>`.
        gate_mode: `wait`, `orchestrate`, or `auto`. See `orchestrating-a-run`.
        capacity_key: Join or move it into a shared run limit.
        capacity_limit: Change how many schedules sharing `capacity_key` may run at once.
        blackout: Change its daily quiet hours.
        catch_up: Whether a held fire fires once immediately once clear, rather than waiting.

    Returns:
        The schedule as stored after the change, as a line of words.

    Raises:
        ModelRetry: `schedule_id` names no schedule, `workflow_id` does not check out here, an
            argument is not what its parameter declares, or `cadence`, `gate_policy`, or `blackout`
            is not one of its own shapes.
    """
    schedule_uuid = _parse_id(schedule_id)
    session = ctx.deps
    changes = ScheduleUpdate(
        workflow_id=workflow_id,
        arguments=arguments,
        cadence=cadence,
        enabled=enabled,
        gate_policy=gate_policy,
        gate_mode=gate_mode,
        capacity_key=capacity_key,
        capacity_limit=capacity_limit,
        blackout=blackout,
        catch_up=catch_up,
    )
    try:
        updated = await scheduler.update_schedule(session.library, session.registry, schedule_uuid, changes, names=names)
    except ScheduleNotFoundError as exc:
        raise ModelRetry(UNKNOWN_SCHEDULE) from exc
    except WorkflowUnavailableError as exc:
        msg = f"cannot point this schedule at {exc.workflow_id}. {exc.reason}"
        raise ModelRetry(msg) from exc
    except (WorkflowNotRunnableError, WorkflowParametersMissingError) as exc:
        msg = f"cannot point this schedule at {exc.workflow_id}. {exc}"
        raise ModelRetry(msg) from exc
    except AdmissionRefused as exc:
        msg = f"this schedule cannot hold those values. {exc}"
        raise ModelRetry(msg) from exc
    except ScheduleSettingInvalidError as exc:
        raise ModelRetry(str(exc)) from exc
    return f"Updated schedule {_describe(updated)}"


async def delete_schedule(scheduler: Scheduler, schedule_id: str) -> str:
    """Remove a standing schedule for good. A run it already started keeps going.

    Args:
        scheduler: The scheduler that holds the schedule.
        schedule_id: The schedule's own id, as shown by `schedules`.

    Returns:
        A sentence confirming the removal.

    Raises:
        ModelRetry: `schedule_id` names no schedule.
    """
    schedule_uuid = _parse_id(schedule_id)
    try:
        await scheduler.delete_schedule(schedule_uuid)
    except ScheduleNotFoundError as exc:
        raise ModelRetry(UNKNOWN_SCHEDULE) from exc
    return f"Deleted schedule {schedule_uuid}. Any run it already started keeps going."


async def run_schedule_now(scheduler: Scheduler, sessions: PooledSessions, run: RunCollaborators, schedule_id: str) -> str:
    """Fire a standing schedule immediately, through the same path its own cadence uses.

    Held (in words, not forced through) exactly as a normal tick would be by an overlap, a full
    capacity pool, or a blackout window.

    Args:
        scheduler: The scheduler that holds the schedule.
        sessions: The pool the fire's own runner session is opened in.
        run: Where the fired run's records land, where it announces, and which engine runs it.
        schedule_id: The schedule's own id, as shown by `schedules`.

    Returns:
        A sentence naming the schedule and what the fire did.

    Raises:
        ModelRetry: `schedule_id` names no schedule.
    """
    schedule_uuid = _parse_id(schedule_id)
    try:
        outcome = await scheduler.run_now(await sessions.pool(), schedule_uuid, run=run)
    except ScheduleNotFoundError as exc:
        raise ModelRetry(UNKNOWN_SCHEDULE) from exc
    return f"Fired schedule {schedule_uuid}. {outcome}."
