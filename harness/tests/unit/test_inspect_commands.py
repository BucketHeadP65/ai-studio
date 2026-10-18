"""Inspection by hand: one run under every watch that selects it, or every run that ended in a window."""

from datetime import UTC, datetime
from uuid import UUID

from core.ai.history.contracts import EventAtPositionRow, RunRow
from core.ai.inspection.contracts import JobLanes, Sweep, Watch
from core.cqrs.contracts import MemoryJobs

from harness.features.inspection.domain import Issued
from harness.features.inspection.service import issue_for_run, issue_since
from tests.support.fakes.inspection import FakeRecordEvents

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
WATCHES = (Watch(name="every-run", rules=("duplicate_call",)), Watch(name="agent-b", agent="agent-b", rules=("duplicate_call",)))


class Runs:
    def __init__(self, rows: dict[str, RunRow]) -> None:
        self.rows = rows

    async def run(self, run_id: str) -> RunRow | None:
        return self.rows.get(run_id)


def a_run(run_id: str, agent: str) -> RunRow:
    return RunRow(
        run_id=run_id, conversation_id=UUID(int=1), parent_run_id=None, agent_name=agent, started_at=T0, metadata={}, registration_id=None
    )


async def test_a_run_is_issued_under_every_watch_that_selects_it_whatever_its_when() -> None:
    lanes = JobLanes(MemoryJobs())

    issued = await issue_for_run("run-1", runs=Runs({"run-1": a_run("run-1", "agent-a")}), watches=WATCHES, lanes=lanes)
    again = await issue_for_run("run-1", runs=Runs({"run-1": a_run("run-1", "agent-a")}), watches=WATCHES, lanes=lanes)

    assert issued == [Issued(watch="every-run", job_id=1, created=True)]
    assert again == [Issued(watch="every-run", job_id=1, created=False)]


async def test_an_unknown_run_answers_none() -> None:
    assert await issue_for_run("nope", runs=Runs({}), watches=WATCHES, lanes=JobLanes(MemoryJobs())) is None


async def test_a_window_runs_every_watch_over_it() -> None:
    events = FakeRecordEvents(
        [
            EventAtPositionRow(
                id=1, run_id="run-1", kind="run_completed", step_index=0, at=T0, agent_name="agent-b", conversation_id=UUID(int=1)
            )
        ]
    )
    lanes = JobLanes(MemoryJobs())
    sweep = Sweep(watches=WATCHES, events=events, lanes=lanes, clock=lambda: T0)

    passes = await issue_since(24, sweep=sweep)

    assert [(name, passed.considered, passed.enqueued) for name, passed in passes] == [("every-run", 1, 1), ("agent-b", 1, 1)]
