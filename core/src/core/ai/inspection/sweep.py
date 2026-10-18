"""Reconcile terminal events for every watch over its window, including commits the dispatch missed.

What the dispatch cannot see (an event committed behind its position, a run that ended while nothing consumed the
record) the sweep finds by time. Only a successful command naming the terminal event proves the final transcript was
processed. Earlier or abandoned commands do not suppress it, and a live command coalesces until the next sweep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from core.ai.inspection.dispatch import selected, source_of
from core.cqrs.contracts import utcnow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from core.ai.inspection.domain.watches import Watch
    from core.ai.inspection.internal_contracts import RecordEvents, SweptLanes
    from core.cqrs.contracts import Clock


@dataclass(frozen=True)
class SweepPass:
    """What one sweep did: runs in the window the watch selected, commands it issued."""

    considered: int
    enqueued: int


@dataclass(frozen=True)
class Sweep:
    """The watches with a window, over the runs that ended in it."""

    watches: Sequence[Watch]
    events: RecordEvents
    lanes: SweptLanes
    clock: Clock = utcnow
    limit: int = 1000

    async def window(self, watch: Watch, hours: int, *, now: datetime | None = None, through_position: int | None = None) -> SweepPass:
        """Reconcile one watch over the terminal events of a fixed range. Newer arrivals wait for the next pass.

        Args:
            watch: The watch whose selection decides which runs count.
            hours: How far back from `now` the window reaches.
            now: The end of the window, or `None` for the clock's time.
            through_position: The last event position the pass reads, or `None` for the record's last position.

        Returns:
            How many runs the watch selected and how many commands the pass issued.
        """
        since = (now or self.clock()) - timedelta(hours=hours)
        through = await self.events.last_position() if through_position is None else through_position
        considered = enqueued = 0
        position = 0
        while position < through:
            page = await self.events.ended_since(since, limit=self.limit, after_position=position)
            if not page:
                break
            for event in page:
                if event.id > through:
                    break
                if not selected(watch, event):
                    continue
                considered += 1
                source = source_of(event)
                if await self.lanes.inspected(watch.name, source, terminal_event=event.id):
                    continue
                answer = await self.lanes.inspect_run(source, watch.name, terminal_event=event.id)
                enqueued += int(answer.created)
            position = page[-1].id
        return SweepPass(considered=considered, enqueued=enqueued)

    async def tick(self) -> SweepPass:
        """Reconcile every watch over its sweep window, or its live-reconciliation window when it has no sweep window.

        Returns:
            The runs considered and the commands issued, summed over every watch.
        """
        considered = enqueued = 0
        now = self.clock()
        through = await self.events.last_position()
        for watch in self.watches:
            hours = watch.sweep_hours if watch.sweep_hours is not None else watch.reconcile_hours
            passed = await self.window(watch, hours, now=now, through_position=through)
            considered += passed.considered
            enqueued += passed.enqueued
        return SweepPass(considered=considered, enqueued=enqueued)
