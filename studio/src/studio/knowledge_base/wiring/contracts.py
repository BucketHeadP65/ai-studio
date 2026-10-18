"""The read-only engine, the two database gateways and the other wiring types that modules outside the wiring package import."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from engram.retrieval.contracts import ReadOnlySqlGateway, Reranking

if TYPE_CHECKING:
    from engram.retrieval.contracts import SqlKnowledgeBase
    from sqlalchemy.ext.asyncio import AsyncEngine


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
    """The re-ranking studio's searches run through, or `None` when it is off or OpenRouter has no key."""

    reranking: Reranking | None


@dataclass(frozen=True)
class EvaluationSqlTools:
    """The SQL tools an evaluation run with the SQL switch gives the agent under test, under their own type.

    They send a refused statement back to the model as a retry. Only a caller that asks for this type gets that behavior.
    """

    capability: SqlKnowledgeBase[object]
