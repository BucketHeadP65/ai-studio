"""The worker over the workflow library's outbox, against a real, migrated database.

`migrated` (engram's own testcontainers plugin) runs the repo's one alembic chain to head in a fresh database, which
holds `coordination.outbox`, `coordination.event_deliveries` and `public.schedules`, as the library's database does.
The rows are written before the worker exists, the way an import in another process leaves them while no server
runs. Skips the whole tier when Docker is unavailable.
"""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
import pytest_asyncio

from core.ai.workflows.contracts import ReusableStepSaved
from core.cqrs.contracts import ImmediateWake, PostgresOutbox
from core.db import build_engine
from engram.testing.infrastructure.knowledge_base_db import migrated, pg  # noqa: F401 (imported to be used as pytest fixtures, never called directly)

from harness.adapters.store import LibraryOutbox, ScheduleDatabase
from harness.features.scheduling.contracts import StepChangeArming
from harness.features.scheduling.domain import Schedule
from harness.features.scheduling.infrastructure.postgres import PostgresSchedules
from harness.features.scheduling.service import Scheduler
from harness.main import Driving, library_event_worker
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.serving import a_served_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
    from uuid import UUID

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _asyncpg_dsn(url: str) -> str:
    """The `migrated` fixture's SQLAlchemy-style URL, in the plain form asyncpg wants."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _saved(step_id: str) -> ReusableStepSaved:
    """The event a new reusable step version raises, naming `step_id`."""
    name, _, version = step_id.removeprefix("st.").rpartition(".v")
    return ReusableStepSaved(step=name, version=int(version), step_id=step_id, step_hash="sha256:0", saved_by="import", superseded=("st.x.v1",))


class _Library:
    """The schedules and the outbox of one migrated database, opened together and closed together."""

    def __init__(self, pool: asyncpg.Pool, outbox: PostgresOutbox) -> None:
        self.schedules = PostgresSchedules(pool)
        self.outbox = outbox
        self.pool = pool

    def worker_arming_at(self, moment: datetime) -> tuple[StepChangeArming, Driving]:
        """The arming the server builds over these schedules, and how its worker is driven, with the clock at `moment`."""
        arming = StepChangeArming(Scheduler(self.schedules, self.schedules, FakeBroadcaster()), clock=lambda: moment)
        return arming, Driving(wake=ImmediateWake(), name="harness:test", clock=lambda: moment)


@pytest_asyncio.fixture
async def library(migrated: str) -> AsyncIterator[_Library]:  # noqa: F811 (pytest resolves the imported fixture by this name)
    pool = await asyncpg.create_pool(_asyncpg_dsn(migrated))
    engine = build_engine(migrated)
    try:
        yield _Library(pool, PostgresOutbox(engine))
    finally:
        await engine.dispose()
        await pool.close()


async def _outbox_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT event_id, event_type, delivered_at FROM coordination.outbox ORDER BY id")


async def _subscribers(pool: asyncpg.Pool, event_id: UUID) -> list[str]:
    rows = await pool.fetch("SELECT subscriber FROM coordination.event_deliveries WHERE event_id = $1", event_id)
    return [row["subscriber"] for row in rows]


async def test_a_step_save_written_while_no_worker_runs_is_delivered_once_the_worker_starts(library: _Library) -> None:
    waiting = Schedule(workflow_id="wf.step-maintenance", cadence="on step change quiet 10m")
    await library.schedules.insert(waiting)
    event = _saved("st.digest_source.v12")
    await library.outbox.append([event])

    arming, driving = library.worker_arming_at(T0)
    worked = await library_event_worker(arming, library.outbox, driving).tick()

    assert worked.deliveries == 1
    stored = await library.schedules.get(waiting.id)
    assert stored is not None
    assert (stored.next_run_at, stored.pending_step_ids) == (T0 + timedelta(minutes=10), ("st.digest_source.v12",))
    (row,) = await _outbox_rows(library.pool)
    assert (row["event_id"], row["event_type"]) == (event.event_id, "ReusableStepSaved")
    assert row["delivered_at"] is not None, "the row is closed"
    assert await _subscribers(library.pool, event.event_id) == ["schedules.on_step_change"], "one delivery, recorded by the arm itself"


async def test_two_saves_of_one_step_arm_one_fire_that_holds_the_step_once(library: _Library) -> None:
    waiting = Schedule(workflow_id="wf.step-maintenance", cadence="on step change quiet 10m")
    await library.schedules.insert(waiting)
    await library.outbox.append([_saved("st.digest_source.v12")])
    first_arming, first_driving = library.worker_arming_at(T0)
    await library_event_worker(first_arming, library.outbox, first_driving).tick()
    await library.outbox.append([_saved("st.digest_source.v12")])
    later = T0 + timedelta(minutes=3)

    second_arming, second_driving = library.worker_arming_at(later)
    await library_event_worker(second_arming, library.outbox, second_driving).tick()

    stored = await library.schedules.get(waiting.id)
    assert stored is not None
    assert (stored.next_run_at, stored.pending_step_ids) == (later + timedelta(minutes=10), ("st.digest_source.v12",))


async def test_a_step_save_with_no_on_step_change_schedule_is_closed_and_arms_nothing(library: _Library) -> None:
    on_index = Schedule(workflow_id="wf.stances", cadence="on index docs")
    await library.schedules.insert(on_index)
    event = _saved("st.digest_source.v12")
    await library.outbox.append([event])

    arming, driving = library.worker_arming_at(T0)
    worker = library_event_worker(arming, library.outbox, driving)
    first = await worker.tick()
    second = await worker.tick()

    assert (first.deliveries, second.deliveries) == (1, 0)
    (row,) = await _outbox_rows(library.pool)
    assert row["delivered_at"] is not None
    assert await _subscribers(library.pool, event.event_id) == ["schedules.on_step_change"]
    later = Schedule(workflow_id="wf.step-maintenance", cadence="on step change")
    await library.schedules.insert(later)
    await library_event_worker(arming, library.outbox, driving).tick()
    for schedule in (on_index, later):
        stored = await library.schedules.get(schedule.id)
        assert stored is not None
        assert (stored.next_run_at, stored.pending_step_ids, stored.pending_page_ids) == (None, (), ()), "a consumed event never arms a later schedule"


async def test_a_delivery_the_arm_recorded_before_a_stop_is_closed_on_the_next_pass_without_arming_again(library: _Library) -> None:
    """The arm committed its record, then the worker stopped before it closed the row: the next pass only closes it."""
    waiting = Schedule(workflow_id="wf.step-maintenance", cadence="on step change quiet 10m")
    await library.schedules.insert(waiting)
    event = _saved("st.digest_source.v12")
    await library.outbox.append([event])
    await library.pool.execute(
        "INSERT INTO coordination.event_deliveries (event_id, subscriber) VALUES ($1, 'schedules.on_step_change')", event.event_id
    )

    arming, driving = library.worker_arming_at(T0)
    worked = await library_event_worker(arming, library.outbox, driving).tick()

    assert worked.deliveries == 1
    (row,) = await _outbox_rows(library.pool)
    assert row["delivered_at"] is not None, "the open row is closed"
    assert await _subscribers(library.pool, event.event_id) == ["schedules.on_step_change"], "no second delivery is recorded"
    stored = await library.schedules.get(waiting.id)
    assert stored is not None
    assert (stored.next_run_at, stored.pending_step_ids) == (None, ()), "the schedule is not armed again"

def _library_event_loops() -> list[asyncio.Task[object]]:
    return [task for task in asyncio.all_tasks() if task.get_name().endswith(":library-events")]


async def test_the_server_lifespan_delivers_a_waiting_step_save_and_stops_its_loop(library: _Library, tmp_path: Path) -> None:
    """The whole lifespan over the test leaves, its outbox and schedules on the throwaway database."""
    waiting = Schedule(workflow_id="wf.step-maintenance", cadence="on step change quiet 10m")
    await library.schedules.insert(waiting)
    event = _saved("st.digest_source.v12")
    await library.outbox.append([event])
    answering = {LibraryOutbox: LibraryOutbox(library.outbox), ScheduleDatabase: ScheduleDatabase(library.pool)}
    before = datetime.now(UTC)

    async with a_served_client(state=tmp_path, answering=answering):
        delivered_at = None
        for _ in range(500):
            (row,) = await _outbox_rows(library.pool)
            delivered_at = row["delivered_at"]
            if delivered_at is not None:
                break
            await asyncio.sleep(0.02)
        assert delivered_at is not None, "the server's library worker never delivered the waiting row"
        assert len(_library_event_loops()) == 1, "the server runs one library event loop"
    assert _library_event_loops() == [], "the loop outlived the server"

    stored = await library.schedules.get(waiting.id)
    assert stored is not None
    assert stored.pending_step_ids == ("st.digest_source.v12",)
    assert stored.next_run_at is not None
    assert stored.next_run_at >= before + timedelta(minutes=10), "the quiet period counts from the delivery"
    assert await _subscribers(library.pool, event.event_id) == ["schedules.on_step_change"]
