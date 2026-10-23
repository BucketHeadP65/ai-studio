"""The review of pending disclosure summaries in `kb.unit_summaries`, shared by the CLI and the HTTP API.

A summary is disclosed only once it is approved. Approval changes the status and nothing else.
The statement factories build each statement without a database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, update

from engram.indexing.infrastructure.orm import UnitSummaryORM
from engram.indexing.read_models import PendingSummary

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Select, Update
    from sqlalchemy.ext.asyncio import AsyncEngine


class SummaryGovernance:
    """Lists and approves one collection's pending summaries."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Bind the review to one engine.

        Args:
            engine: The engine every statement runs on.
        """
        self._engine = engine

    @staticmethod
    def pending_statement(collection: str) -> Select[str, str, str, str, str, datetime]:
        """Select `collection`'s pending rows, oldest first.

        Args:
            collection: The collection to read.

        Returns:
            The select of the pending summaries.
        """
        return (
            select(
                UnitSummaryORM.unit_id,
                UnitSummaryORM.unit_kind,
                UnitSummaryORM.collection,
                UnitSummaryORM.summary,
                UnitSummaryORM.model,
                UnitSummaryORM.indexed_at,
            )
            .where(UnitSummaryORM.collection == collection, UnitSummaryORM.status == "pending")
            .order_by(UnitSummaryORM.indexed_at)
        )

    @staticmethod
    def approve_statement(collection: str) -> Update:
        """Promote `collection`'s pending rows to `active`.

        Args:
            collection: The collection to update.

        Returns:
            The update statement.
        """
        return (
            update(UnitSummaryORM)
            .where(UnitSummaryORM.collection == collection, UnitSummaryORM.status == "pending")
            .values(status="active")
        )

    async def list_pending(self, collection: str) -> list[PendingSummary]:
        """Return `collection`'s pending summaries, oldest first.

        Args:
            collection: The collection to read.

        Returns:
            The pending summaries.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(self.pending_statement(collection))
            return [PendingSummary.model_validate(row, from_attributes=True) for row in result.all()]

    async def approve_pending(self, collection: str) -> int:
        """Promote every pending summary of `collection` to `active` and commit.

        Args:
            collection: The collection to update.

        Returns:
            The number of rows updated.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(self.approve_statement(collection))
            return result.rowcount
