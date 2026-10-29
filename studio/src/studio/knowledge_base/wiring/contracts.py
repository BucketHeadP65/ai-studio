"""The read-only engine, the two database gateways and the other wiring types that modules outside the wiring package import."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from asyncpg.exceptions import InvalidCatalogNameError, InvalidPasswordError
from engram.retrieval.contracts import ReadOnlySqlGateway, Reranking

if TYPE_CHECKING:
    from engram.retrieval.contracts import SqlKnowledgeBase
    from sqlalchemy.ext.asyncio import AsyncEngine


# A dead port and an unknown host raise an `OSError` subtype (`ConnectionRefusedError`, `socket.gaierror`), a wrong
# password and a wrong database name raise an asyncpg error. A query error is not one of them, so a missing schema
# still reaches the preflight.
UNREACHABLE_ERRORS: tuple[type[Exception], ...] = (OSError, InvalidPasswordError, InvalidCatalogNameError)
"""The errors a failed connection to Postgres raises."""


def unreachable_cause(error: BaseException) -> Exception | None:
    """The error in `error`'s chain of causes that says Postgres could not be reached, or `None` when none does.

    SQLAlchemy wraps asyncpg's refusal of a password or of a database name in a `DBAPIError`, so a caller catching
    `UNREACHABLE_ERRORS` and `DBAPIError` passes what it caught here and re-raises when this answers `None`.

    Args:
        error: The error a connection or a read raised.

    Returns:
        The first of `UNREACHABLE_ERRORS` in the chain, `error` itself included.
    """
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, UNREACHABLE_ERRORS):
            return current
        current = current.__cause__
    return None


class ReaderEngine:
    """The read-only engine, under its own type so the container tells it apart from the admin engine.

    `engine` holds the read-only engine.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Hold the read-only engine.

        Args:
            engine: The engine that connects as the read-only database role.
        """
        self.engine = engine


class ReaderSqlGateway(ReadOnlySqlGateway):
    """The read-only gateway over the read-only engine, under its own type so the container tells it apart from the admin gateway.

    It reads as no knowledge base until a caller binds it to one knowledge base's collection.
    """


class AdminSqlGateway(ReadOnlySqlGateway):
    """The read-only gateway over the admin engine, which reads the `kb.*` tables the read-only role is not granted.

    It behaves exactly as `ReaderSqlGateway` and has its own type only so the container tells the two apart.
    """


@dataclass(frozen=True)
class SearchReranking:
    """The re-ranking studio's searches run through, or `None` when it is off or the gateway has no key."""

    reranking: Reranking | None


@dataclass(frozen=True)
class EvaluationSqlTools:
    """The SQL tools an evaluation run with the SQL switch gives the agent under test, under their own type.

    They send a refused statement back to the model as a retry. Only a caller that asks for this type gets that behavior.
    """

    capability: SqlKnowledgeBase[object]
