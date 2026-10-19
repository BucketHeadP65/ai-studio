"""The scheduling agent tools: list, create, edit, delete, and fire a schedule now, in conversation.

Exercises each tool directly (`RunContext(deps=session, model=TestModel(), usage=RunUsage())`),
the same seam `test_message_worker.py` uses, so a tool runs exactly as the agent would call it.
Reuses `test_schedule_ticker.py`'s `FakeWorkflowStore` and `tests.support.fakes.schedules.FakeSchedules`
rather than a second copy of either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

from core.ai.workflows.contracts import StepRegistry, WorkflowArguments
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.features.running.records import RunCollaborators
from harness.features.scheduling.contracts import (
    Scheduler,
    delete_schedule,
    run_schedule_now,
    schedule_workflow,
    schedules,
    update_schedule,
)
from harness.features.scheduling.domain import UNKNOWN_SCHEDULE
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import RunSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.runtime import a_runtime
from tests.support.state import a_throwaway_state, records_under
from tests.unit.test_schedule_ticker import FakeWorkflowStore, _one_node_spec, _schedule, _typed_registry, _typed_spec

if TYPE_CHECKING:
    from pathlib import Path


def _ctx(session: Runtime) -> RunContext[Runtime]:
    return RunContext(deps=session, model=TestModel(), usage=RunUsage())


def _world(store: FakeWorkflowStore, *, registry: StepRegistry | None = None) -> SessionPool:
    """A session and the pool holding it, the way the composition root composes them."""
    session = Runtime(registry=registry if registry is not None else StepRegistry(), library=store, agent=cast("Any", None), session_log=NoSessionLog(), state=a_throwaway_state())
    return SessionPool(session)
async def test_schedules_lists_nothing_when_none_exist() -> None:
    assert await schedules(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster())) == "No schedules exist yet."


async def test_schedules_lists_one_line_per_schedule_with_the_stated_facts() -> None:
    store = FakeSchedules()
    plain = _schedule(workflow_id="wf-sales")
    fancy = _schedule(
        workflow_id="wf-sales", capacity_key="pool-a", capacity_limit=2, blackout="22:00-06:00", catch_up=True, enabled=False
    )
    await store.insert(plain)
    await store.insert(fancy)
    report = await schedules(Scheduler(store, store, FakeBroadcaster()))

    assert str(plain.id) in report
    assert str(fancy.id) in report
    plain_line = next(line for line in report.splitlines() if str(plain.id) in line)
    fancy_line = next(line for line in report.splitlines() if str(fancy.id) in line)
    assert "workflow wf-sales" in plain_line
    assert "cadence every 30m" in plain_line
    assert "enabled" in plain_line
    assert "capacity" not in plain_line
    assert "disabled" in fancy_line
    assert "capacity pool-a limit 2" in fancy_line
    assert "blackout 22:00-06:00" in fancy_line
    assert "catch up on" in fancy_line


async def test_schedule_workflow_creates_and_names_the_next_three_fires(registry: StepRegistry) -> None:
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    schedule_rows = FakeSchedules()
    session = _world(store, registry=registry).default
    report = await schedule_workflow(Scheduler(schedule_rows, schedule_rows, FakeBroadcaster()), _ctx(session), "wf-sales", "every 30m", arguments={"quarter": "Q1"}, run_engine="in_process")

    assert "Scheduled wf-sales as" in report
    assert "Next three fires" in report
    rows = await schedule_rows.list_all()
    assert len(rows) == 1
    created = rows[0]
    assert created.workflow_id == "wf-sales"
    assert created.cadence == "every 30m"
    assert created.arguments == {"quarter": "Q1"}
    assert created.next_run_at is not None
    assert str(created.id) in report


async def test_schedule_workflow_carries_the_new_knobs_through(registry: StepRegistry) -> None:
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    schedule_rows = FakeSchedules()
    session = _world(store, registry=registry).default
    await schedule_workflow(
        Scheduler(schedule_rows, schedule_rows, FakeBroadcaster()),
        _ctx(session),
        "wf-sales",
        "every 30m",
        arguments={"quarter": "Q1"},
        capacity_key="pool-a",
        capacity_limit=2,
        blackout="22:00-06:00",
        catch_up=True,
        run_engine="in_process",
    )

    created = (await schedule_rows.list_all())[0]
    assert created.capacity_key == "pool-a"
    assert created.capacity_limit == 2
    assert created.blackout == "22:00-06:00"
    assert created.catch_up is True


async def test_schedule_workflow_refuses_a_workflow_that_does_not_check_out() -> None:
    session = _world(FakeWorkflowStore()).default
    with pytest.raises(ModelRetry, match="cannot schedule no-such-workflow"):
        await schedule_workflow(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), _ctx(session), "no-such-workflow", "every 30m", run_engine="in_process")


async def test_schedule_workflow_refuses_a_cadence_that_is_not_one(registry: StepRegistry) -> None:
    session = _world(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), registry=registry).default
    with pytest.raises(ModelRetry, match="cadence looks like"):
        await schedule_workflow(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), _ctx(session), "wf-sales", "whenever", arguments={"quarter": "Q1"}, run_engine="in_process")


async def test_schedule_workflow_refuses_a_blackout_that_is_not_one(registry: StepRegistry) -> None:
    session = _world(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), registry=registry).default
    with pytest.raises(ModelRetry, match="blackout looks like"):
        await schedule_workflow(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), _ctx(session), "wf-sales", "every 30m", arguments={"quarter": "Q1"}, blackout="whenever", run_engine="in_process")


async def test_update_schedule_changes_only_the_given_fields(registry: StepRegistry) -> None:
    store = FakeSchedules()
    schedule = _schedule()
    await store.insert(schedule)
    session = _world(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), registry=registry).default
    report = await update_schedule(
        Scheduler(store, store, FakeBroadcaster()), _ctx(session), str(schedule.id), enabled=False, capacity_key="pool-a", capacity_limit=1, run_engine="in_process"
    )

    assert "Updated schedule" in report
    updated = await store.get(schedule.id)
    assert updated is not None
    assert updated.enabled is False
    assert updated.capacity_key == "pool-a"
    assert updated.capacity_limit == 1
    assert updated.workflow_id == "wf-sales", "an untouched field survives a partial update"


async def test_update_schedule_refuses_an_unknown_id() -> None:
    session = _world(FakeWorkflowStore()).default
    with pytest.raises(ModelRetry, match=UNKNOWN_SCHEDULE):
        await update_schedule(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), _ctx(session), str(uuid4()), enabled=False, run_engine="in_process")


async def test_update_schedule_refuses_a_malformed_id() -> None:
    session = _world(FakeWorkflowStore()).default
    with pytest.raises(ModelRetry, match=UNKNOWN_SCHEDULE):
        await update_schedule(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), _ctx(session), "not-a-uuid", enabled=False, run_engine="in_process")


async def test_update_schedule_refuses_a_cadence_that_is_not_one(registry: StepRegistry) -> None:
    store = FakeSchedules()
    schedule = _schedule()
    await store.insert(schedule)
    session = _world(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), registry=registry).default
    with pytest.raises(ModelRetry, match="cadence looks like"):
        await update_schedule(Scheduler(store, store, FakeBroadcaster()), _ctx(session), str(schedule.id), cadence="whenever", run_engine="in_process")


async def test_delete_schedule_removes_the_row() -> None:
    store = FakeSchedules()
    schedule = _schedule()
    await store.insert(schedule)
    report = await delete_schedule(Scheduler(store, store, FakeBroadcaster()), str(schedule.id))

    assert str(schedule.id) in report
    assert await store.get(schedule.id) is None


async def test_delete_schedule_refuses_an_unknown_id() -> None:
    with pytest.raises(ModelRetry, match=UNKNOWN_SCHEDULE):
        await delete_schedule(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), str(uuid4()))


async def test_run_schedule_now_fires_immediately(registry: StepRegistry, tmp_path: Path) -> None:
    store = FakeSchedules()
    schedule = _schedule(next_run_at=None)  # not due on any clock -- run-now bypasses the scan
    await store.insert(schedule)
    pool = _world(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), registry=registry)

    report = await run_schedule_now(
        Scheduler(store, store, FakeBroadcaster()),
        APool(pool),
        RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings()),
        str(schedule.id),
    )

    assert "launched" in report
    after = await store.get(schedule.id)
    assert after is not None
    fired_session = await pool.get(after.session_id)
    assert fired_session.active.task is not None
    await fired_session.active.task


async def test_run_schedule_now_refuses_an_unknown_id(tmp_path: Path) -> None:
    with pytest.raises(ModelRetry, match=UNKNOWN_SCHEDULE):
        await run_schedule_now(
            Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()),
            APool(SessionPool(a_runtime())),
            RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings()),
            str(uuid4()),
        )


async def test_run_schedule_now_lets_a_key_error_from_inside_the_fire_escape_rather_than_refuse(registry: StepRegistry, tmp_path: Path) -> None:
    store = FakeSchedules(stamp_error=KeyError("inside the fire"))
    pool = _world(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), registry=registry)
    runner = await pool.create(None, session_kind="runner")
    schedule = _schedule(next_run_at=None, session_id=runner.session_id)
    await store.insert(schedule)

    with pytest.raises(KeyError) as raised:
        await run_schedule_now(
            Scheduler(store, store, FakeBroadcaster()),
            APool(pool),
            RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings()),
            str(schedule.id),
        )

    assert raised.type is KeyError
    task = runner.active.task
    assert task is not None
    await task


async def test_the_agent_may_schedule_a_workflow_with_typed_arguments() -> None:
    """The tool takes JSON, so an array reaches the store as an array rather than as its text."""
    store = FakeSchedules()
    pool = _world(FakeWorkflowStore(specs={"wf-typed": _typed_spec()}), registry=_typed_registry([]))
    typed: WorkflowArguments = {"knowledge_bases": ["research", "repo-docs"], "include_rejected": True}

    answer = await schedule_workflow(Scheduler(store, store, FakeBroadcaster()), _ctx(pool.default), "wf-typed", "every 30m", arguments=typed, run_engine="in_process")

    assert "wf-typed" in answer
    kept = (await store.list_all())[0]
    assert kept.arguments == typed


async def test_the_agent_is_told_which_argument_the_schema_refused() -> None:
    store = FakeSchedules()
    pool = _world(FakeWorkflowStore(specs={"wf-typed": _typed_spec()}), registry=_typed_registry([]))

    with pytest.raises(ModelRetry, match="knowledge_bases"):
        await schedule_workflow(
            Scheduler(store, store, FakeBroadcaster()), _ctx(pool.default), "wf-typed", "every 30m", arguments={"knowledge_bases": ["a", "b", "c", "d"], "include_rejected": True},
            run_engine="in_process",
        )


async def test_schedule_workflow_announces_the_new_schedule(registry: StepRegistry) -> None:
    topics = FakeBroadcaster()
    session = _world(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), registry=registry).default
    await schedule_workflow(Scheduler(FakeSchedules(), FakeSchedules(), topics), _ctx(session), "wf-sales", "every 30m", arguments={"quarter": "Q1"}, run_engine="in_process")

    assert topics.published == ["schedules"]


async def test_update_schedule_announces_the_change(registry: StepRegistry) -> None:
    store = FakeSchedules()
    schedule = _schedule()
    await store.insert(schedule)
    topics = FakeBroadcaster()
    session = _world(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), registry=registry).default
    await update_schedule(Scheduler(store, store, topics), _ctx(session), str(schedule.id), enabled=False, run_engine="in_process")

    assert topics.published == ["schedules"]


async def test_delete_schedule_announces_the_removal() -> None:
    store = FakeSchedules()
    schedule = _schedule()
    await store.insert(schedule)
    topics = FakeBroadcaster()
    await delete_schedule(Scheduler(store, store, topics), str(schedule.id))

    assert topics.published == ["schedules"]


async def test_update_schedule_refuses_an_unknown_id_before_checking_its_fields() -> None:
    session = _world(FakeWorkflowStore()).default
    with pytest.raises(ModelRetry, match=UNKNOWN_SCHEDULE):
        await update_schedule(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), _ctx(session), str(uuid4()), cadence="whenever", run_engine="in_process")


async def test_schedule_workflow_says_why_this_app_cannot_run_the_workflow() -> None:
    session = _world(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})).default

    with pytest.raises(ModelRetry, match=r"^cannot schedule wf-sales\. this app cannot run it\. "):
        await schedule_workflow(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), _ctx(session), "wf-sales", "every 30m", run_engine="in_process")


async def test_schedule_workflow_names_the_required_parameters_left_unfilled(registry: StepRegistry) -> None:
    session = _world(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), registry=registry).default

    with pytest.raises(ModelRetry, match=r"^cannot schedule wf-sales\. fill in: quarter$"):
        await schedule_workflow(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), _ctx(session), "wf-sales", "every 30m", run_engine="in_process")
