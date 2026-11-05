"""An in-memory outbox whose first reads fail, the way a database still starting up fails a job runner's tick."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from core.cqrs.contracts import MemoryOutbox

if TYPE_CHECKING:
    from core.cqrs.domain import StoredEvent


class FailingOutbox(MemoryOutbox):
    """A `MemoryOutbox` whose first reads of undelivered events raise `errors` in turn.

    `reads` counts every read, and `read_again` is set by the first read that succeeds, so a test can stop the job
    runner there.
    """

    def __init__(self, *errors: Exception) -> None:
        super().__init__()
        self.errors = list(errors)
        self.reads = 0
        self.read_again = asyncio.Event()

    async def undelivered(self, *, limit: int = 100) -> list[StoredEvent]:
        """Raise the next scripted error, or set `read_again` and read as the in-memory outbox does."""
        self.reads += 1
        if self.errors:
            raise self.errors.pop(0)
        self.read_again.set()
        return await super().undelivered(limit=limit)
