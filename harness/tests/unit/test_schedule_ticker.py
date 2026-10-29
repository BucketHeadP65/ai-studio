"""The scheduler ticker: one due schedule fires once, an overlapping one is skipped and marked.

`FakeWorkflowStore` is the `WorkflowLibrary` protocol's minimal double: `fetch_workflow` is the only
verb the ticker actually calls.
"""

from __future__ import annotations

import asyncio

from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

from core.ai.workflows.contracts import FromNode, FromParameter, LibraryError, ParameterSpec, RunNode, Workflow

from harness.features.running import service as running_service
from harness.features.running.contracts import schedule_runner
from harness.features.running.records import RunCollaborators
from harness.features.scheduling.domain import CHANGED_PAGES_PARAMETER, Schedule, ScheduleUpdate
from harness.features.scheduling.exceptions import ScheduleSettingInvalidError
from harness.features.scheduling.service import CAUGHT_UP_OUTCOME, ENGINE_UNREACHABLE_OUTCOME, MISSED_OUTCOME, Scheduler
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import RunSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.state import a_throwaway_state, records_under

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import Lineage, SavedWorkflow, StepRegistry, WorkflowFingerprint, WorkflowPresentation


@dataclass
class FakeWorkflowStore:
    """`WorkflowLibrary`'s minimal double: `fetch_workflow` and `current` are the verbs the ticker calls.

    `newest` scripts `current`: an id it maps answers with the newer id, any other answers itself.
    """

    specs: dict[str, Workflow] = field(default_factory=dict)
    newest: dict[str, str] = field(default_factory=dict)
    cards: dict[str, SavedWorkflow] = field(default_factory=dict)
    """The card each id answers with, for a caller that reads the author's own words. An id absent
    here answers `None`, the way a library holding no card for it does."""
    card_error: Exception | None = None
    """Raised by every card read when set: a library that holds cards but cannot serve one."""

    async def current(self, workflow_id: str) -> str:
        return self.newest.get(workflow_id, workflow_id)

    async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
        del task, limit
        return ()

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        try:
            return self.specs[workflow_id]
        except KeyError:
            msg = f"no workflow {workflow_id!r} here"
            raise LibraryError(msg) from None

    async def put(self, workflow: Workflow, **_kwargs: object) -> str:
        del workflow
        msg = "FakeWorkflowStore does not save"
        raise NotImplementedError(msg)

    async def redescribe(
        self, workflow_id: str, *, title: str, summary: str, applicability: str, presentation: WorkflowPresentation | None = None
    ) -> None:
        del workflow_id, title, summary, applicability, presentation
        msg = "FakeWorkflowStore does not redescribe"
        raise NotImplementedError(msg)

    async def fetch_fingerprint(self, workflow_id: str) -> WorkflowFingerprint | None:
        del workflow_id
        return None

    async def fetch_lineage(self, workflow_id: str) -> Lineage | None:
        del workflow_id
        return None

    async def fetch_card(self, workflow_id: str) -> SavedWorkflow | None:
        if self.card_error is not None:
            raise self.card_error
        return self.cards.get(workflow_id)

    async def locate(self, name: str, version: int) -> str | None:
        del name, version
        return None

    async def set_archived(self, workflow_id: str, *, archived: bool) -> None:
        del workflow_id, archived
        msg = "FakeWorkflowStore does not archive"
        raise NotImplementedError(msg)

    async def archived(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        del limit
        return ()

    async def catalog(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        del limit
        return ()

    async def catalog_size(self) -> int:
        return len(self.specs)


def _one_node_spec(name: str = "wf-sales") -> Workflow:
    """A single, gateless step: launches and finishes with nothing for a person to answer."""
    return Workflow(
        name=name,
        version=1,
        description="fetch sales",
        parameters={"quarter": ParameterSpec(description="fiscal quarter", json_schema={"type": "string"})},
        nodes=(RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),),
        output=FromNode(node="fetch_sales"),
    )


def _register_slow_step(registry: StepRegistry, hold: asyncio.Event) -> Workflow:
    """Register a one-step workflow whose step blocks on `hold`, so a run stays live until released."""

    async def slow_step() -> str:
        await hold.wait()
        return "done"

    registry.register(slow_step)
    return Workflow(
        name="wf-hold",
        version=1,
        description="one step blocked on an event, so a test can hold a run live across ticks",
        nodes=(RunNode(id="slow_step", work="slow_step", inputs={}),),
        output=FromNode(node="slow_step"),
    )


def _schedule(**overrides: object) -> Schedule:
    defaults: dict[str, object] = {
        "workflow_id": "wf-sales",
        "cadence": "every 30m",
        "arguments": {"quarter": "Q1"},
        "next_run_at": datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Schedule(**defaults)  # type: ignore[arg-type]


def _runtime(registry: StepRegistry, store: FakeWorkflowStore) -> Runtime:
    return Runtime(registry=registry, library=store, agent=cast("Any", None), session_log=NoSessionLog(), state=a_throwaway_state())


async def _settle(task: asyncio.Task[None] | None) -> None:
    """Cancel and await a launched run's task, so nothing dangles past the test."""
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.fixture(autouse=True)
def _the_engine_answers_unless_a_test_says_otherwise() -> Iterator[None]:
    """The reachability flag is process state the composition root installs. Reset it around each test."""
    running_service.install_engine_reachable(reachable=True)
    yield
    running_service.install_engine_reachable(reachable=True)


async def test_a_fire_that_cannot_reach_the_engine_is_stamped_rather_than_lost(registry: StepRegistry, tmp_path: Path) -> None:
    """A schedule whose engine is down steps a tick log an owner can read, and comes due again."""
    records = records_under(tmp_path)
    on_temporal = RunSettings(run_engine="temporal")
    running_service.install_engine_reachable(reachable=False)
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=on_temporal), now=now)

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == ENGINE_UNREACHABLE_OUTCOME
    assert after.next_run_at == now + timedelta(minutes=30), "the schedule keeps its cadence, and the engine coming back is enough"
    session = await pool.get(after.session_id)
    assert session.active.task is None, "nothing was launched"


async def test_an_orchestrate_schedule_is_refused_when_saved_on_temporal(registry: StepRegistry) -> None:
    schedules = FakeSchedules()

    with pytest.raises(ScheduleSettingInvalidError, match="Temporal, where an orchestrated gate"):
        await Scheduler(schedules, schedules, FakeBroadcaster()).create_schedule(
            FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}),
            registry,
            workflow_id="wf-sales",
            arguments={"quarter": "Q1"},
            cadence="every 30m",
            gate_policy="wait",
            gate_mode="orchestrate",
            capacity_key=None,
            capacity_limit=None,
            blackout=None,
            catch_up=False,
            names=None,
            now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
            run_engine="temporal",
        )

    assert await schedules.list_all() == [], "a refused schedule is never stored"


async def test_changing_a_schedule_to_orchestrate_is_refused_on_temporal(registry: StepRegistry) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)

    with pytest.raises(ScheduleSettingInvalidError, match="wait or auto"):
        await Scheduler(schedules, schedules, FakeBroadcaster()).update_schedule(
            FakeWorkflowStore(), registry, schedule.id, ScheduleUpdate(gate_mode="orchestrate"), names=None, run_engine="temporal"
        )

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.gate_mode == "wait"


async def test_an_orchestrate_schedule_already_stored_on_temporal_is_stamped_refused_rather_than_raising(registry: StepRegistry, tmp_path: Path) -> None:
    """A schedule saved before the refusal existed fires into a stamped refusal, never an error out of the fire."""
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(gate_mode="orchestrate")
    await schedules.insert(schedule)
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    on_temporal = RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings(run_engine="temporal"))

    outcome = await Scheduler(schedules, schedules, FakeBroadcaster()).run_now(pool, schedule.id, run=on_temporal, now=now)

    assert outcome.startswith("refused: ")
    assert "Temporal" in outcome
    assert outcome.endswith("Save the schedule in wait or auto mode.")
    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == outcome
    assert after.next_run_at == now + timedelta(minutes=30)
    session = await pool.get(after.session_id)
    assert session.active.task is None, "nothing was launched"


async def test_a_due_schedule_launches_once_and_restamps(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=now)

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == "launched"
    assert after.last_run_at == now
    assert after.next_run_at == now + timedelta(minutes=30)
    assert after.session_id != ""
    session = await pool.get(after.session_id)
    assert session.active.workflow_id == "wf-sales"
    assert session.active.task is not None
    await session.active.task  # the step has no gate to wait at; let it actually finish
    assert "Finished" in session.active.result


async def test_a_fire_records_the_runner_its_spend_is_read_back_under(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC))

    after = await schedules.get(schedule.id)
    assert after is not None
    session = await pool.get(after.session_id)
    assert session.active.task is not None
    await session.active.task
    assert session.active.run_id is not None
    header = records.spend.read(session.active.run_id, "wf-sales")
    assert header is not None
    assert header.runner == schedule_runner(str(schedule.id))


async def test_a_schedule_fires_the_newest_live_version_of_its_workflow(registry: StepRegistry, tmp_path: Path) -> None:
    """A schedule points at the workflow, not the version it was made against: v2 saved later is what fires."""
    records = records_under(tmp_path)
    store = FakeWorkflowStore(
        specs={"wf.sales.v1": _one_node_spec("sales"), "wf.sales.v2": _one_node_spec("sales")},
        newest={"wf.sales.v1": "wf.sales.v2"},
    )
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf.sales.v1")
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC))

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == "launched"
    assert after.workflow_id == "wf.sales.v1", "the row keeps the id it was written with; resolution happens on every fire"
    session = await pool.get(after.session_id)
    assert session.active.workflow_id == "wf.sales.v2"
    await _settle(session.active.task)


async def test_a_schedules_own_auto_gate_mode_crosses_the_gate_unattended(registry: StepRegistry, quarterly_report: Workflow, tmp_path: Path) -> None:
    """A schedule's own setting reaches the session: nobody is watching a cron fire."""
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf.gated": quarterly_report})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf.gated", gate_mode="auto")
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC))

    after = await schedules.get(schedule.id)
    assert after is not None
    session = await pool.get(after.session_id)
    assert session.active.gate_mode == "auto"
    task = session.active.task
    assert task is not None
    await task
    assert session.active.pending is None, "automode never holds the gate for a person"
    assert "Finished" in session.active.result


async def test_a_schedules_own_gate_mode_orchestrate_reaches_the_session(registry: StepRegistry, quarterly_report: Workflow, tmp_path: Path) -> None:
    """The mode a schedule was saved with is the mode its own runner session launches under.

    No decider is wired on this bare registry, so the converted gate waits exactly as `"wait"`
    would -- this proves the wiring reaches the session, not the decision itself, which the
    orchestrator-gate suite already covers end to end.
    """
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf.gated": quarterly_report})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf.gated", gate_mode="orchestrate")
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC))

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == "launched"
    session = await pool.get(after.session_id)
    assert session.active.gate_mode == "orchestrate"
    for _ in range(500):
        if session.active.pending is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the run never waiting")
    await _settle(session.active.task)


async def test_an_overlapping_tick_records_missed(registry: StepRegistry, quarterly_report: Workflow, tmp_path: Path) -> None:
    """A cadence short enough to come due again before the gated run finishes is skipped, not queued."""
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf.gated": quarterly_report})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf.gated", cadence="every 1s")
    await schedules.insert(schedule)
    first_tick = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    second_tick = first_tick + timedelta(seconds=2)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=first_tick)
    launched = await schedules.get(schedule.id)
    assert launched is not None
    assert launched.last_outcome == "launched"
    assert launched.next_run_at is not None
    assert launched.next_run_at <= second_tick

    # The gate no browser answers keeps the run alive on the clock the assertions read, whatever
    # `second_tick` the tick itself is told it is.
    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=second_tick)

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == MISSED_OUTCOME
    assert after.session_id == launched.session_id, "the same runner session is reused, not a second one minted"
    session = await pool.get(after.session_id)
    await _settle(session.active.task)


async def test_run_now_launches_immediately(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(next_run_at=None)  # not due on any clock -- run-now bypasses the scan
    await schedules.insert(schedule)

    outcome = await Scheduler(schedules, schedules, FakeBroadcaster()).run_now(
        pool, schedule.id, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    )

    assert outcome == "launched"
    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == "launched"
    session = await pool.get(after.session_id)
    assert session.active.task is not None
    await session.active.task


async def test_run_now_of_an_unknown_schedule_raises(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    pool = SessionPool(_runtime(registry, FakeWorkflowStore()))
    with pytest.raises(KeyError):
        await Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()).run_now(pool, uuid4(), run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()))


async def test_turning_a_schedule_off_stamps_its_pause_with_the_schedulers_clock(registry: StepRegistry) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    paused = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)

    await Scheduler(schedules, schedules, FakeBroadcaster()).update_schedule(
        FakeWorkflowStore(), registry, schedule.id, ScheduleUpdate(enabled=False), names=None, run_engine="in_process", now=paused
    )

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.enabled is False
    assert after.paused_at == paused


async def test_a_workflow_that_stops_validating_is_refused_with_the_validators_words(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    broken = _one_node_spec(name="wf-broken").model_copy(update={"nodes": (RunNode(id="ghost", work="not_registered", inputs={}),)})
    store = FakeWorkflowStore(specs={"wf-broken": broken})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf-broken")
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC))

    after = await schedules.get(schedule.id)
    assert after is not None
    assert "not_registered" in after.last_outcome
    assert after.last_outcome.startswith("refused:")


async def test_a_poisoned_cadence_fails_only_that_schedule_and_the_ticker_survives(registry: StepRegistry, tmp_path: Path) -> None:
    """A stored cadence `parse_cadence` cannot read must not end the tick or the next one."""
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    poisoned = _schedule(workflow_id="wf-missing", cadence="not a cadence at all")
    healthy = _schedule(workflow_id="wf-sales")
    await schedules.insert(poisoned)
    await schedules.insert(healthy)
    first_tick = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=first_tick)

    poisoned_after = await schedules.get(poisoned.id)
    assert poisoned_after is not None
    assert poisoned_after.last_outcome.startswith("failed, "), "the poisoned schedule is stamped with why, not left silent"
    assert poisoned_after.next_run_at is None, "a schedule that cannot compute its next fire stops coming due, not tight-loops"

    healthy_after = await schedules.get(healthy.id)
    assert healthy_after is not None
    assert healthy_after.last_outcome == "launched", "the healthy schedule still fires in the same pass"
    session = await pool.get(healthy_after.session_id)
    assert session.active.task is not None
    await session.active.task

    # A second tick, over what is due now, must not raise either -- the ticker survives past the
    # schedule that just failed.
    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=first_tick + timedelta(minutes=31))
    still_poisoned = await schedules.get(poisoned.id)
    assert still_poisoned is not None
    assert still_poisoned.last_run_at == poisoned_after.last_run_at, "no next_run_at means it never comes due again on its own"
    relaunched = await schedules.get(healthy.id)
    assert relaunched is not None
    second = (await pool.get(relaunched.session_id)).active.task
    assert second is not None
    await second


# --- shared capacity pools -----------------------------------------------------------------


async def test_two_schedules_sharing_capacity_the_second_holds_until_the_first_finishes(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    hold = asyncio.Event()
    held_spec = _register_slow_step(registry, hold)
    store = FakeWorkflowStore(specs={"wf-hold": held_spec, "wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    first = _schedule(workflow_id="wf-hold", cadence="every 1h", arguments={}, capacity_key="pool-a", capacity_limit=1)
    second = _schedule(workflow_id="wf-sales", cadence="every 1h", capacity_key="pool-a", capacity_limit=1)
    await schedules.insert(first)
    await schedules.insert(second)
    t0 = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=t0)

    first_after = await schedules.get(first.id)
    second_after = await schedules.get(second.id)
    assert first_after is not None
    assert second_after is not None
    assert first_after.last_outcome == "launched"
    assert second_after.last_outcome == "held, capacity pool-a is full"
    assert second_after.next_run_at == second.next_run_at, "a capacity hold never advances next_run_at"

    # The first is still live (blocked on `hold`); the second is still due (never advanced) and
    # holds again on the next tick.
    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=t0 + timedelta(minutes=5))
    still_held = await schedules.get(second.id)
    assert still_held is not None
    assert still_held.last_outcome == "held, capacity pool-a is full"

    # Release the first run and let it actually finish -- capacity frees.
    hold.set()
    first_session = await pool.get(first_after.session_id)
    assert first_session.active.task is not None
    await first_session.active.task

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=t0 + timedelta(minutes=10))
    freed = await schedules.get(second.id)
    assert freed is not None
    assert freed.last_outcome == "launched"
    freed_session = await pool.get(freed.session_id)
    assert freed_session.active.task is not None
    await freed_session.active.task


async def test_a_schedule_without_a_capacity_key_is_never_held(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule()  # no capacity_key, no capacity_limit
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC))

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == "launched"
    session = await pool.get(after.session_id)
    assert session.active.task is not None
    await session.active.task


# --- blackout windows -----------------------------------------------------------------------


async def test_a_fire_inside_a_blackout_window_is_suppressed_and_advances_past_it(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(
        workflow_id="wf-sales", cadence="every 1h", blackout="22:00-06:00", next_run_at=datetime(2026, 8, 24, 23, 0, tzinfo=UTC)
    )
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=datetime(2026, 8, 24, 23, 0, tzinfo=UTC))

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == "blacked out until 06:00"
    assert after.next_run_at == datetime(2026, 8, 25, 6, 0, tzinfo=UTC), "advances to the cadence's next occurrence outside the window"


async def test_catch_up_holds_through_a_blackout_and_fires_once_when_it_ends(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(
        workflow_id="wf-sales",
        cadence="every 1h",
        blackout="22:00-06:00",
        catch_up=True,
        next_run_at=datetime(2026, 8, 24, 23, 0, tzinfo=UTC),
    )
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=datetime(2026, 8, 24, 23, 0, tzinfo=UTC))
    held = await schedules.get(schedule.id)
    assert held is not None
    assert held.last_outcome == "blacked out until 06:00"
    assert held.next_run_at == datetime(2026, 8, 24, 23, 0, tzinfo=UTC), "catch_up holds next_run_at in place rather than skipping ahead"

    # Still inside the window an hour later -- held again, still in place.
    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=datetime(2026, 8, 25, 0, 0, tzinfo=UTC))
    still_held = await schedules.get(schedule.id)
    assert still_held is not None
    assert still_held.last_outcome == "blacked out until 06:00"
    assert still_held.next_run_at == held.next_run_at

    # The window has ended -- fires once, immediately, stamped caught up.
    fire_moment = datetime(2026, 8, 25, 6, 0, tzinfo=UTC)
    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=fire_moment)
    caught_up = await schedules.get(schedule.id)
    assert caught_up is not None
    assert caught_up.last_outcome == CAUGHT_UP_OUTCOME
    assert caught_up.next_run_at == fire_moment + timedelta(hours=1), "next_run_at resumes normal cadence math from the catch-up fire"
    session = await pool.get(caught_up.session_id)
    assert session.active.task is not None
    await session.active.task


# --- catch-up (overlap) ---------------------------------------------------------------------


async def test_catch_up_fires_once_immediately_when_an_overlap_clears(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    hold = asyncio.Event()
    held_spec = _register_slow_step(registry, hold)
    store = FakeWorkflowStore(specs={"wf-hold": held_spec})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf-hold", cadence="every 1s", arguments={}, catch_up=True)
    await schedules.insert(schedule)
    t0 = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=t0)
    launched = await schedules.get(schedule.id)
    assert launched is not None
    assert launched.last_outcome == "launched"
    assert launched.next_run_at == t0 + timedelta(seconds=1)

    # Due again, still overlapping (the step never returned) -- held, and multiple such misses
    # collapse into the one fire that eventually happens.
    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=t0 + timedelta(seconds=2))
    held_after = await schedules.get(schedule.id)
    assert held_after is not None
    assert held_after.last_outcome == MISSED_OUTCOME
    assert held_after.next_run_at == launched.next_run_at, "catch_up holds next_run_at in place rather than skipping ahead"

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=t0 + timedelta(seconds=3))
    still_held = await schedules.get(schedule.id)
    assert still_held is not None
    assert still_held.last_outcome == MISSED_OUTCOME
    assert still_held.next_run_at == launched.next_run_at

    hold.set()
    session = await pool.get(still_held.session_id)
    assert session.active.task is not None
    await session.active.task  # let the step actually finish, clearing the overlap

    fire_moment = t0 + timedelta(seconds=4)
    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=fire_moment)
    caught_up = await schedules.get(schedule.id)
    assert caught_up is not None
    assert caught_up.last_outcome == CAUGHT_UP_OUTCOME
    assert caught_up.next_run_at == fire_moment + timedelta(seconds=1), "next_run_at resumes normal cadence math from the catch-up fire"
    fired_session = await pool.get(caught_up.session_id)
    assert fired_session.active.task is not None
    await fired_session.active.task


async def test_catch_up_false_leaves_the_missed_behavior_unchanged(registry: StepRegistry, quarterly_report: Workflow, tmp_path: Path) -> None:
    """Same shape as `test_an_overlapping_tick_records_missed`, with `catch_up` explicit and false."""
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf.gated": quarterly_report})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf.gated", cadence="every 1s", catch_up=False)
    await schedules.insert(schedule)
    first_tick = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    second_tick = first_tick + timedelta(seconds=2)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=first_tick)
    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()), now=second_tick)

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == MISSED_OUTCOME
    assert after.next_run_at == second_tick + timedelta(seconds=1), "without catch_up, a miss still skips ahead past itself"
    session = await pool.get(after.session_id)
    await _settle(session.active.task)


def _typed_spec() -> Workflow:
    """A schedule's own workflow, declaring an array and a boolean beside its string."""
    return Workflow(
        name="wf-typed",
        version=1,
        description="read several knowledge bases, optionally including what was ruled out",
        parameters={
            "knowledge_bases": ParameterSpec(
                description="which knowledge bases to read",
                json_schema={"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3, "uniqueItems": True},
            ),
            "include_rejected": ParameterSpec(description="keep the options ruled out", json_schema={"type": "boolean"}),
        },
        nodes=(
            RunNode(
                id="read",
                work="read_bases",
                inputs={
                    "knowledge_bases": FromParameter(parameter="knowledge_bases"),
                    "include_rejected": FromParameter(parameter="include_rejected"),
                },
            ),
        ),
        output=FromNode(node="read"),
    )


def _typed_registry(seen: list[tuple[list[str], bool]]) -> StepRegistry:
    """A registry whose one step records exactly what a fire handed it."""
    from core.ai.workflows.contracts import StepRegistry as Registry

    registry = Registry()

    async def read_bases(knowledge_bases: list[str], include_rejected: bool) -> str:
        seen.append((knowledge_bases, include_rejected))
        return ",".join(knowledge_bases)

    registry.register(read_bases)
    return registry


async def test_a_fire_hands_the_step_the_stored_json_rather_than_its_text(tmp_path: Path) -> None:
    """A stored array stays an array and a stored boolean stays a boolean, all the way into the step."""
    seen: list[tuple[list[str], bool]] = []
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf-typed": _typed_spec()})
    pool = SessionPool(_runtime(_typed_registry(seen), store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf-typed", arguments={"knowledge_bases": ["research", "repo-docs"], "include_rejected": True})
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(
        pool,
        run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()),
        now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    )

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == "launched"
    session = await pool.get(after.session_id)
    assert session.active.task is not None
    await session.active.task
    assert seen == [(["research", "repo-docs"], True)], "never the string of a list, never the string 'True'"


async def test_a_fire_whose_arguments_no_longer_fit_is_stamped_rather_than_launched(tmp_path: Path) -> None:
    """A stored value the workflow's own schema refuses stops at the tick log, naming the parameter."""
    seen: list[tuple[list[str], bool]] = []
    records = records_under(tmp_path)
    store = FakeWorkflowStore(specs={"wf-typed": _typed_spec()})
    pool = SessionPool(_runtime(_typed_registry(seen), store))
    schedules = FakeSchedules()
    schedule = _schedule(
        workflow_id="wf-typed", arguments={"knowledge_bases": ["a", "b", "c", "d"], "include_rejected": True}
    )
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(
        pool,
        run=RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings()),
        now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    )

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome.startswith("refused: ")
    assert "knowledge_bases" in after.last_outcome
    assert seen == []


# --- on-index fires ---------------------------------------------------------------------------


def _sales_taking_pages() -> Workflow:
    """`_one_node_spec` with the changed pages parameter an on-index schedule passes its pages to."""
    sales = _one_node_spec()
    changed_pages = ParameterSpec(description="the changed pages", json_schema={"type": "string"}, required=False)
    return sales.model_copy(update={"parameters": {**sales.parameters, "changed_page_ids": changed_pages}})


def _pages_spec(name: str = "wf-pages") -> Workflow:
    """A workflow whose one step reads the changed pages a fire passes."""
    return Workflow(
        name=name,
        version=1,
        description="read the pages an index changed",
        parameters={CHANGED_PAGES_PARAMETER: ParameterSpec(description="the changed pages", json_schema={"type": "string"}, required=False)},
        nodes=(RunNode(id="read", work="read_pages", inputs={"changed_page_ids": FromParameter(parameter=CHANGED_PAGES_PARAMETER)}),),
        output=FromNode(node="read"),
    )


def _pages_registry(seen: list[str], hold: asyncio.Event | None = None) -> StepRegistry:
    """A registry whose one step records the changed pages a fire handed it, and waits for `hold` when given."""
    from core.ai.workflows.contracts import StepRegistry as Registry

    registry = Registry()

    async def read_pages(changed_page_ids: str) -> str:
        seen.append(changed_page_ids)
        if hold is not None:
            await hold.wait()
        return changed_page_ids

    registry.register(read_pages)
    return registry


async def test_an_on_index_fire_passes_the_changed_pages_sorted(tmp_path: Path) -> None:
    seen: list[str] = []
    store = FakeWorkflowStore(specs={"wf-pages": _pages_spec()})
    pool = SessionPool(_runtime(_pages_registry(seen), store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf-pages", arguments={}, cadence="on index notes", pending_page_ids=("b.two", "a.one"))
    await schedules.insert(schedule)
    run = RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings())

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=run, now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC))

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == "launched"
    assert (after.next_run_at, after.pending_page_ids) == (None, ()), "the fire rests and every page it passed on leaves the schedule"
    session = await pool.get(after.session_id)
    assert session.active.task is not None
    await session.active.task
    assert seen == ["a.one,b.two"]


async def test_a_held_on_index_fire_keeps_its_pages_and_its_fire(tmp_path: Path) -> None:
    seen: list[str] = []
    hold = asyncio.Event()
    store = FakeWorkflowStore(specs={"wf-pages": _pages_spec()})
    pool = SessionPool(_runtime(_pages_registry(seen, hold), store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf-pages", arguments={}, cadence="on index notes", pending_page_ids=("a.one",))
    await schedules.insert(schedule)
    scheduler = Scheduler(schedules, schedules, FakeBroadcaster())
    run = RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings())
    first_fire = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    second_fire = first_fire + timedelta(minutes=30)

    await scheduler.tick(pool, run=run, now=first_fire)
    await schedules.arm_with_pages({schedule.id: second_fire}, event_id=uuid4(), page_ids=["c.three"])
    await scheduler.tick(pool, run=run, now=second_fire)

    held = await schedules.get(schedule.id)
    assert held is not None
    assert held.last_outcome == MISSED_OUTCOME
    assert (held.next_run_at, held.pending_page_ids) == (second_fire, ("c.three",)), "the overlap keeps the arm and the pages it collected"
    hold.set()
    session = await pool.get(held.session_id)
    assert session.active.task is not None
    await session.active.task
    assert seen == ["a.one"], "only the first fire passed pages on"


async def test_a_refused_on_index_fire_keeps_its_pages(registry: StepRegistry, tmp_path: Path) -> None:
    """The newest version dropped the parameter, so the fire refuses and the pages wait for a fire that can pass them on."""
    store = FakeWorkflowStore(
        specs={"wf.pages.v1": _pages_spec("pages"), "wf.pages.v2": _one_node_spec("pages")}, newest={"wf.pages.v1": "wf.pages.v2"}
    )
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf.pages.v1", cadence="on index notes", pending_page_ids=("a.one",))
    await schedules.insert(schedule)
    run = RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings())

    outcome = await Scheduler(schedules, schedules, FakeBroadcaster()).run_now(pool, schedule.id, run=run, now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC))

    assert outcome.startswith("refused: ")
    assert outcome == "refused: wf.pages.v2 declares no changed_page_ids"
    after = await schedules.get(schedule.id)
    assert after is not None
    assert (after.next_run_at, after.pending_page_ids) == (None, ("a.one",)), "the schedule rests and keeps its pages"
    session = await pool.get(after.session_id)
    assert session.active.task is None, "nothing was launched"


@pytest.mark.parametrize("cadence", ["on index other", "every 30m"], ids=["another knowledge base", "a cadence that is not on index"])
async def test_a_cadence_change_clears_the_pending_pages(cadence: str) -> None:
    """Pages collected from one knowledge base's indexes never ride a fire of another knowledge base or of the clock."""
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf-pages", arguments={}, cadence="on index notes", pending_page_ids=("a.one",))
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).update_schedule(
        FakeWorkflowStore(specs={"wf-pages": _pages_spec()}),
        _pages_registry([]),
        schedule.id,
        ScheduleUpdate(cadence=cadence),
        names=None,
        run_engine="in_process",
    )

    after = await schedules.get(schedule.id)
    assert after is not None
    assert (after.cadence, after.pending_page_ids) == (cadence, ())


@pytest.mark.parametrize(
    "changes",
    [
        ScheduleUpdate(enabled=False),
        ScheduleUpdate(cadence="on index notes"),
        ScheduleUpdate(cadence="On Index notes"),
        ScheduleUpdate(cadence="on index notes quiet 10m"),
    ],
    ids=["no cadence", "the same cadence", "the same cadence spelled otherwise", "only another quiet period"],
)
async def test_a_change_that_keeps_the_knowledge_base_keeps_the_pending_pages(changes: ScheduleUpdate) -> None:
    """A change that still waits on the same knowledge base's indexes, such as one of its quiet period alone, keeps what it collected."""
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf-pages", arguments={}, cadence="on index notes", pending_page_ids=("a.one",))
    await schedules.insert(schedule)

    await Scheduler(schedules, schedules, FakeBroadcaster()).update_schedule(
        FakeWorkflowStore(specs={"wf-pages": _pages_spec()}), _pages_registry([]), schedule.id, changes, names=None, run_engine="in_process"
    )

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.pending_page_ids == ("a.one",)
