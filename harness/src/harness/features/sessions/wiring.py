"""What the sessions slice registers: the port over the pool, the projection, the project index, and the settle's write-behind."""

from __future__ import annotations

from core.cqrs.contracts import TopicSink
from wireup import injectable

from harness.adapters.contracts import SessionsDatabase
from harness.features.sessions.infrastructure.postgres import PostgresProjectionPool
from harness.features.sessions.internal_contracts import ProjectionPool
from harness.features.sessions.service import ProjectedIndex, SessionProjection, live_refresh
from harness.session import ProjectIndex, SessionRefresh


@injectable
def projection_pool(database: SessionsDatabase) -> ProjectionPool:
    """The port the projection reads and writes through, over the pool this deployment opened.

    Args:
        database: The sessions database this deployment opened.

    Returns:
        The projection's database port.
    """
    return PostgresProjectionPool(database.pool)


@injectable
def projection(pool: ProjectionPool) -> SessionProjection:
    """The five projected tables the studio's session list answers from.

    Args:
        pool: The projection's database port.

    Returns:
        The session projection.
    """
    return SessionProjection(pool)


@injectable
def project_index(pool: ProjectionPool) -> ProjectIndex:
    """Which project owns a session, and where it lives, read off the projected rows.

    Args:
        pool: The projection's database port.

    Returns:
        The project index.
    """
    return ProjectedIndex(pool)


@injectable
def session_refresh(pool: ProjectionPool, topics: TopicSink) -> SessionRefresh:
    """How a settled turn refreshes its own session's rows, written behind the answer.

    Args:
        pool: The projection's database port.
        topics: Where the change to the session list is announced.

    Returns:
        The refresh a settled turn schedules.
    """
    return live_refresh(pool, topics)


REGISTRATIONS: list[object] = [projection_pool, projection, project_index, session_refresh]
"""Everything this slice puts in the container, each under the type its consumers declare."""
