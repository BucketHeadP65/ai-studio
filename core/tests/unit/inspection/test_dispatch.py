"""The dispatch: events after the cursor become InspectRun commands for the watches that select them."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic_ai_harness.step_persistence import EventKind

from core.ai.history.contracts import EventAtPositionRow
from core.ai.inspection.dispatch import CONSUMER, Dispatch, selected, triggers
from core.ai.inspection.domain.watches import Watch
from core.ai.inspection.lanes import INSPECT_RUN_KIND, INSPECTOR_AGENT_NAME, JobLanes
from core.ai.inspection.sweep import Sweep
from core.cqrs.contracts import MemoryJobs
from tests.support.fakes.inspection import FakeCursors, FakeRecordEvents

AT = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
CONVERSATION = UUID("00000000-0000-0000-0000-000000000001")
EVERY_RUN = Watch(name="every-run", rules=("duplicate_call",))
EACH_STEP = Watch(name="agent-a-steps", agent="agent-a", rules=("duplicate_call",), when="each_step")
SWEPT = Watch(name="nightly", rules=("duplicate_call",), when="sweep", sweep_hours=24)


def event(event_id: int, kind: EventKind, run_id: str = "run-1", agent: str | None = "agent-a") -> EventAtPositionRow:
    return EventAtPositionRow(id=event_id, run_id=run_id, kind=kind, step_index=0, at=AT, agent_name=agent, conversation_id=CONVERSATION)


def test_a_run_end_triggers_run_end_watches_and_each_step_watches_but_never_a_sweep() -> None:
    ended = event(1, "run_completed")
    assert triggers(EVERY_RUN, ended)
    assert triggers(EACH_STEP, ended)
    assert not triggers(SWEPT, ended)


def test_a_step_triggers_only_each_step_watches_that_select_the_run() -> None:
    step = event(2, "model_request_completed")
    assert not triggers(EVERY_RUN, step)
    assert triggers(EACH_STEP, step)
    assert not triggers(EACH_STEP, event(3, "model_request_completed", agent="agent-b"))


async def test_a_fresh_consumer_starts_at_the_records_last_position_and_issues_nothing() -> None:
    events = FakeRecordEvents([event(5, "run_completed"), event(6, "run_completed", run_id="run-2")])
    cursors = FakeCursors()
    jobs = MemoryJobs()

    passed = await Dispatch(watches=(EVERY_RUN,), events=events, cursors=cursors, lanes=JobLanes(jobs)).tick()

    assert (passed.read, passed.enqueued, passed.position) == (0, 0, 6)
    assert cursors.positions == {CONSUMER: 6}
    assert await jobs.claim([INSPECT_RUN_KIND], worker="w") is None


async def test_events_after_the_cursor_become_one_command_per_selecting_watch() -> None:
    events = FakeRecordEvents(
        [event(7, "model_request_completed"), event(8, "run_completed"), event(9, "run_failed", run_id="run-2", agent="agent-b")]
    )
    cursors = FakeCursors(positions={CONSUMER: 6})
    jobs = MemoryJobs()

    passed = await Dispatch(watches=(EVERY_RUN, EACH_STEP, SWEPT), events=events, cursors=cursors, lanes=JobLanes(jobs)).tick()

    assert (passed.read, passed.enqueued, passed.position) == (3, 3, 9)
    keys = sorted(job.key for job in jobs.jobs.values())
    assert keys == ["agent-a-steps:record:run-1", "every-run:record:run-1", "every-run:record:run-2"]
    assert cursors.positions[CONSUMER] == 9


async def test_a_pass_with_nothing_new_leaves_the_cursor_alone() -> None:
    cursors = FakeCursors(positions={CONSUMER: 9})

    passed = await Dispatch(
        watches=(EVERY_RUN,), events=FakeRecordEvents([event(9, "run_completed")]), cursors=cursors, lanes=JobLanes(MemoryJobs())
    ).tick()

    assert (passed.read, passed.enqueued, passed.position) == (0, 0, 9)


async def test_a_terminal_commit_behind_the_dispatch_cursor_is_reconciled_for_a_live_watch() -> None:
    events = FakeRecordEvents([event(9, "run_completed", run_id="other")])
    cursors = FakeCursors(positions={CONSUMER: 9})
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)
    events.events.append(event(8, "run_completed", run_id="late"))

    assert (await Dispatch(watches=(EVERY_RUN,), events=events, cursors=cursors, lanes=lanes).tick()).read == 0
    await Sweep(watches=(EVERY_RUN,), events=events, lanes=lanes, clock=lambda: AT).tick()

    late = [job for job in jobs.jobs.values() if job.key == "every-run:record:late"]
    assert len(late) == 1
    assert late[0].payload["terminal_event"] == 8


def test_the_inspectors_own_runs_never_trigger_a_watch() -> None:
    own = event(4, "run_completed", run_id="inspector-1", agent=INSPECTOR_AGENT_NAME)
    assert not triggers(EVERY_RUN, own)
    assert not triggers(Watch(name="the-inspector", agent=INSPECTOR_AGENT_NAME, rules=("duplicate_call",)), own)


def test_selected_is_the_watch_and_never_the_inspector() -> None:
    own = event(5, "run_completed", run_id="inspector-1", agent=INSPECTOR_AGENT_NAME)
    assert not selected(EVERY_RUN, own)
    assert not selected(Watch(name="the-inspector", agent=INSPECTOR_AGENT_NAME, rules=("duplicate_call",)), own)

    step = event(6, "model_request_completed", agent="agent-a")
    assert selected(EACH_STEP, step)
    assert not selected(EACH_STEP, event(7, "model_request_completed", agent="agent-b"))
