"""The record's events read over a database whose connections reach the record's tables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.history.contracts import reads
from core.ai.inspection.dispatch import DISPATCH_KINDS

if TYPE_CHECKING:
    from datetime import datetime

    from core.ai.history.contracts import Database, EventAtPositionRow


@dataclass(frozen=True)
class RecordEventsOverDatabase:
    """The record's events read on a database whose connections reach the record's tables."""

    database: Database

    async def events_after(self, position: int, *, limit: int) -> list[EventAtPositionRow]:
        """The events past `position` a watch acts on, oldest first, at most `limit`.

        Args:
            position: The last position already read.
            limit: The most events to return.

        Returns:
            The events.
        """
        async with self.database.acquire() as connection:
            return await reads.events_after(connection, position, kinds=DISPATCH_KINDS, limit=limit)

    async def last_position(self) -> int:
        """The record's current position, 0 when it holds no event.

        Returns:
            The position.
        """
        async with self.database.acquire() as connection:
            return await reads.last_event_position(connection)

    async def ended_since(self, since: datetime, *, limit: int, after_position: int = 0) -> list[EventAtPositionRow]:
        """The event that ended each run at or after `since`, oldest first, at most `limit`.

        Args:
            since: The earliest end time to include.
            limit: The most events to return.
            after_position: Only events past this position.

        Returns:
            The ending events.
        """
        async with self.database.acquire() as connection:
            return await reads.runs_ended_since(connection, since, limit=limit, after_position=after_position)
