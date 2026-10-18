"""`SqlAlchemyCutPlans`, the stored cut plans in `kb.cut_plans`, serving both the write and the read port.

Each call runs in a transaction of its own over the admin engine, apart from the index's load. A plan is inserted once
and never changed, so an insert keeps any plan already stored under the same key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from core.db import batches
from sqlalchemy import Text, bindparam, func, select
from sqlalchemy.dialects.postgresql import ARRAY, insert

from engram.indexing.infrastructure.orm import CutPlanORM

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from sqlalchemy import Select
    from sqlalchemy.dialects.postgresql import Insert
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["CutPlanRow", "SqlAlchemyCutPlans"]


class CutPlanRow(TypedDict):
    """One `kb.cut_plans` row as the insert writes it."""

    collection: str
    text_hash: str
    planner: str
    starts: list[int]


class SqlAlchemyCutPlans:
    """The `CutPlans` and `CutPlanQueries` over `kb.cut_plans`."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Hold the admin engine every call opens its own transaction on.

        Args:
            engine: The admin engine.
        """
        self._engine = engine

    @staticmethod
    def insert_statement(rows: Sequence[CutPlanRow]) -> Insert:
        """`INSERT INTO kb.cut_plans ... ON CONFLICT DO NOTHING` for `rows`.

        Args:
            rows: The rows to insert. It must not be empty.

        Returns:
            The insert statement.
        """
        return insert(CutPlanORM).values(list(rows)).on_conflict_do_nothing(index_elements=["collection", "text_hash", "planner"])

    @staticmethod
    def select_statement(collection: str, planner: str, text_hashes: Collection[str]) -> Select[tuple[str, list[int]]]:
        """`SELECT text_hash, starts FROM kb.cut_plans` for one knowledge base, one chunk planner and the hashes asked.

        The hashes bind as one array parameter however many there are.

        Args:
            collection: The knowledge base the plans belong to.
            planner: The cut plan identity (`cut_plan_identity`) the plans are stored under.
            text_hashes: The text hashes asked for.

        Returns:
            The select statement.
        """
        hashes = bindparam("text_hashes", sorted(text_hashes), type_=ARRAY(Text()))
        return select(CutPlanORM.text_hash, CutPlanORM.starts).where(
            CutPlanORM.collection == collection, CutPlanORM.planner == planner, CutPlanORM.text_hash.in_(select(func.unnest(hashes)))
        )

    async def insert(self, collection: str, planner: str, plans: Mapping[str, tuple[int, ...]]) -> None:
        """Store the plans in one transaction, keeping any plan already stored under the same key.

        Args:
            collection: The knowledge base the plans belong to.
            planner: The cut plan identity (`cut_plan_identity`) the plans are stored under.
            plans: The paragraph numbers where a new chunk starts, by the content hash of the text each cuts.
        """
        rows = [CutPlanRow(collection=collection, text_hash=text_hash, planner=planner, starts=list(starts)) for text_hash, starts in plans.items()]
        if not rows:
            return
        async with self._engine.begin() as connection:
            for batch in batches(rows):
                await connection.execute(self.insert_statement(batch))

    async def cut_plans(self, collection: str, planner: str, text_hashes: Collection[str]) -> dict[str, tuple[int, ...]]:
        """The stored plans of one knowledge base and one chunk planner for the hashes asked.

        Args:
            collection: The knowledge base the plans belong to.
            planner: The cut plan identity (`cut_plan_identity`) the plans are stored under.
            text_hashes: The content hashes of the texts asked for.

        Returns:
            The paragraph numbers by text hash, leaving out each hash with no stored plan.
        """
        if not text_hashes:
            return {}
        async with self._engine.connect() as connection:
            rows = await connection.execute(self.select_statement(collection, planner, text_hashes))
            return {text_hash: tuple(starts) for text_hash, starts in rows.tuples()}
