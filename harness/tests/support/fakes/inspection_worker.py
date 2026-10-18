"""A model invocation held at an external wait for worker scheduling regressions."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.support.fakes.inspection import FakeRecordEvents

if TYPE_CHECKING:
    from datetime import datetime

    from core.ai.history.contracts import EventAtPositionRow
    from core.ai.inspection.contracts import ScanRun
    from core.cqrs.contracts import JobContext


@dataclass
class FakeWaitingScan:
    """A model invocation held at an external wait until the test releases or cancels it."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, payload: ScanRun, context: JobContext) -> str:
        del payload, context
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.stopped.set()
        return "scanned"


@dataclass
class BlockingReconciliationEvents(FakeRecordEvents):
    """Hold later reconciliation pages while ordinary dispatch remains available."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    reads: int = 0

    async def ended_since(self, since: datetime, *, limit: int, after_position: int = 0) -> list[EventAtPositionRow]:
        self.reads += 1
        if self.reads >= 2:
            self.entered.set()
            await self.release.wait()
        return await super().ended_since(since, limit=limit, after_position=after_position)
