"""The dispatch: a cursor consumer over the record's events that issues `InspectRun` for the watches selecting each run.

The record's `run_events` are the events. The dispatch reads the ones past its position, of the three kinds a watch acts
on, matches each run against the watches, issues one command per selecting watch and advances its position to the last
event read. A consumer that has never run starts at the record's current position and reads nothing older. An event
committed behind a position already read is never seen here. Only the sweep over a window finds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from core.ai.history.contracts import reads
from core.ai.inspection.lanes import INSPECTOR_AGENT_NAME
from core.ai.transcripts.contracts import record_source

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai_harness.step_persistence import EventKind

    from core.ai.history.contracts import EventAtPositionRow
    from core.ai.inspection.domain.watches import Watch
    from core.ai.inspection.internal_contracts import Cursors, InspectRuns, RecordEvents

DISPATCH_KINDS: Final[tuple[EventKind, ...]] = ("model_request_completed", "run_completed", "run_failed")
"""A step landed, or a run ended: the two moments a watch acts on."""
CONSUMER: Final = "inspection.dispatch"
"""The cursor row the dispatch advances."""


@dataclass(frozen=True)
class DispatchPass:
    """What one pass did."""

    read: int
    enqueued: int
    position: int


def source_of(event: EventAtPositionRow) -> str:
    """The transcript source of the run an event belongs to.

    Args:
        event: The record event.

    Returns:
        The run's `record:` source.
    """
    return record_source(event.run_id)


def selected(watch: Watch, event: EventAtPositionRow) -> bool:
    """Whether the watch selects this event's run. The inspector's own runs are never selected.

    Args:
        watch: The watch to match.
        event: The record event.

    Returns:
        True when the watch selects the run.
    """
    if event.agent_name == INSPECTOR_AGENT_NAME:
        return False
    return watch.selects(agent_name=event.agent_name, conversation_id=str(event.conversation_id))


def triggers(watch: Watch, event: EventAtPositionRow) -> bool:
    """Whether the watch acts on this event: a run's end for `run_end`, every step and the end for `each_step`, never for `sweep`.

    Args:
        watch: The watch to match.
        event: The record event.

    Returns:
        True when the watch acts on the event.
    """
    if not selected(watch, event):
        return False
    if watch.when == "run_end":
        return event.kind in reads.TERMINAL_KINDS
    return watch.when == "each_step"


@dataclass(frozen=True)
class Dispatch:
    """One consumer of the record's events, issuing `InspectRun` for the watches that select each run."""

    watches: Sequence[Watch]
    events: RecordEvents
    cursors: Cursors
    lanes: InspectRuns
    consumer: str = CONSUMER
    batch: int = 200

    async def tick(self) -> DispatchPass:
        """Read the events past the position, issue the commands, advance. A first pass only takes the record's position.

        Returns:
            What the pass read, issued and where it left the position.
        """
        position = await self.cursors.position(self.consumer)
        if position is None:
            start = await self.events.last_position()
            await self.cursors.advance(self.consumer, start)
            return DispatchPass(read=0, enqueued=0, position=start)
        found = await self.events.events_after(position, limit=self.batch)
        enqueued = 0
        for event in found:
            for watch in self.watches:
                if triggers(watch, event):
                    terminal_event = event.id if event.kind in reads.TERMINAL_KINDS else None
                    answer = await self.lanes.inspect_run(source_of(event), watch.name, terminal_event=terminal_event)
                    enqueued += int(answer.created)
        last = found[-1].id if found else position
        if last != position:
            await self.cursors.advance(self.consumer, last)
        return DispatchPass(read=len(found), enqueued=enqueued, position=last)
