"""What the scheduling slice registers: the scheduler over the stored schedules, and what hears each run's ending."""

from __future__ import annotations

from core.cqrs.contracts import TopicSink
from wireup import injectable

from harness.adapters.contracts import ScheduleDatabase
from harness.features.running.contracts import RunEndObserver
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
def run_end_observer(scheduler: Scheduler, sessions: PooledSessions) -> RunEndObserver:
    """What hears each workflow run's ending: the scheduler, arming the schedules waiting for that workflow.

    Args:
        scheduler: The scheduler the after-run schedules are armed through.
        sessions: The sessions this process holds, asked for only when a run ends.

    Returns:
        The run-end observer.
    """
    return RunEndArming(scheduler, sessions)


REGISTRATIONS: list[object] = [scheduler, run_end_observer]
"""Everything this slice puts in the container, each under the type its consumers declare."""
