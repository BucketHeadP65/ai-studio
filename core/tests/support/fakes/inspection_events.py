"""Terminal event sources that grow while reconciliation pages through them."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from tests.support.fakes.inspection import FakeRecordEvents

if TYPE_CHECKING:
    from datetime import datetime

    from core.ai.history.contracts import EventAtPositionRow


@dataclass
class GrowingTerminalEvents(FakeRecordEvents):
    """Append a newer terminal event before each page until the scripted ceiling."""

    append_until: int = 8

    async def ended_since(self, since: datetime, *, limit: int, after_position: int = 0) -> list[EventAtPositionRow]:
        last = self.events[-1]
        if last.id < self.append_until:
            self.events.append(replace(last, id=last.id + 1, run_id=f"r{last.id + 1}"))
        return await super().ended_since(since, limit=limit, after_position=after_position)
