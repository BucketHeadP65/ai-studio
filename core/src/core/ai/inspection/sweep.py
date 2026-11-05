"""Reconcile terminal events for every watch over its window, including commits the dispatch missed.

What the dispatch cannot see (an event committed behind its position, a run that ended while nothing consumed the
record) the sweep finds by time. Only a successful command naming the terminal event proves the final transcript was
processed. Earlier or abandoned commands do not suppress it, and a live command coalesces until the next sweep. In a run
already inspected, an open lead whose attribution job was abandoned is asked about once more, and a lead abandoned again
after that is left and logged once per process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from core.ai.inspection.dispatch import selected, source_of
from core.cqrs.contracts import utcnow
from core.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from core.ai.inspection.domain.watches import Watch
    from core.ai.inspection.internal_contracts import LeadQueries, RecordEvents, SweptLanes
    from core.cqrs.contracts import Clock

_logger = get_logger(__name__)


@dataclass(frozen=True)
class SweepPass:
    """What one sweep did: runs in the window the watch selected, commands it issued, attributions it asked for again."""

    considered: int
    enqueued: int
    retried: int


@dataclass(frozen=True)
class Sweep:
    """The watches with a window, over the runs that ended in it."""

    watches: Sequence[Watch]
    events: RecordEvents
    lanes: SweptLanes
    leads: LeadQueries
    clock: Clock = utcnow
    limit: int = 1000
    reported: set[int] = field(default_factory=set)
    """The leads already logged as left after their retry was abandoned, so each is logged once per process."""

    async def window(self, watch: Watch, hours: int, *, now: datetime | None = None, through_position: int | None = None) -> SweepPass:
        """Reconcile one watch over the terminal events of a fixed range. Newer arrivals wait for the next pass.

        In a run already inspected, each open lead whose attribution job was abandoned is asked about once more.

        Args:
            watch: The watch whose selection decides which runs count.
            hours: How far back from `now` the window reaches.
            now: The end of the window, or `None` for the clock's time.
            through_position: The last event position the pass reads, or `None` for the record's last position.

        Returns:
            How many runs the watch selected, how many commands the pass issued and how many attributions it asked for again.
        """
        since = (now or self.clock()) - timedelta(hours=hours)
        through = await self.events.last_position() if through_position is None else through_position
        considered = enqueued = retried = 0
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
                    retried += await self._retry_abandoned_attributions(watch, source)
                    continue
                answer = await self.lanes.inspect_run(source, watch.name, terminal_event=event.id)
                enqueued += int(answer.created)
            position = page[-1].id
        return SweepPass(considered=considered, enqueued=enqueued, retried=retried)

    async def _retry_abandoned_attributions(self, watch: Watch, source: str) -> int:
        """Ask again about each open lead of the run whose attribution job was abandoned, answering how many were issued."""
        left = await self.leads.leads_of(source)
        open_leads = [lead.lead_id for lead in left if lead.watch == watch.name and lead.resolution == "open"]
        settled = await self.leads.settled(open_leads)
        issued = 0
        for lead_id in open_leads:
            if lead_id in settled:
                continue
            outcome = await self.lanes.retry_abandoned_attribution(lead_id)
            if outcome == "left" and lead_id not in self.reported:
                self.reported.add(lead_id)
                _logger.warning(
                    "lead {lead_id} of {source}: its attribution was abandoned again after a retry, left open",
                    lead_id=lead_id,
                    source=source,
                )
            issued += int(outcome == "issued")
        return issued

    async def tick(self) -> SweepPass:
        """Reconcile every watch over its sweep window, or its live-reconciliation window when it has no sweep window.

        Returns:
            The runs considered, the commands issued and the attributions asked for again, summed over every watch.
        """
        considered = enqueued = retried = 0
        now = self.clock()
        through = await self.events.last_position()
        for watch in self.watches:
            hours = watch.sweep_hours if watch.sweep_hours is not None else watch.reconcile_hours
            passed = await self.window(watch, hours, now=now, through_position=through)
            considered += passed.considered
            enqueued += passed.enqueued
            retried += passed.retried
        return SweepPass(considered=considered, enqueued=enqueued, retried=retried)
