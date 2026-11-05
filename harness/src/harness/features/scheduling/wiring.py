"""What the scheduling slice registers: the scheduler over the stored schedules, and the arming each run's ending tells."""

from __future__ import annotations

from core.cqrs.contracts import TopicSink
from wireup import injectable

from harness.adapters.contracts import ScheduleDatabase
from harness.features.scheduling.infrastructure.postgres import PostgresSchedules
from harness.features.scheduling.service import RunEndArming, Scheduler
from harness.session import PooledSessions


@injectable
def scheduler(database: ScheduleDatabase, topics: TopicSink) -> Scheduler:
    """The scheduler, over the schedules in Postgres.

    Args:
        database: The pool the schedules live in.
        topics: Where every change to a schedule is announced.

    Returns:
        The scheduler.
    """
    schedules = PostgresSchedules(database.pool)
    return Scheduler(schedules=schedules, queries=schedules, topics=topics)


@injectable
def run_end_arming(scheduler: Scheduler, sessions: PooledSessions) -> RunEndArming:
    """The arming each workflow run's ending tells, which arms the schedules waiting for that workflow.

    Args:
        scheduler: The scheduler the after-run schedules are armed through.
        sessions: The sessions this process holds, asked for only when a run ends.

    Returns:
        The arming.
    """
    return RunEndArming(scheduler, sessions)


REGISTRATIONS: list[object] = [scheduler, run_end_arming]
"""Everything this slice puts in the container, each under the type its consumers declare."""
