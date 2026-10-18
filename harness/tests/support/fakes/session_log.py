"""A session log that keeps its events in memory, for a test that must not append to a real one."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from harness.domain.events import Recorded

if TYPE_CHECKING:
    from harness.domain.events import SessionEvent


class FakeSessionLog:
    """Keeps every recorded event instead of appending it to a file."""

    def __init__(self) -> None:
        self.recorded: list[SessionEvent] = []

    def record(self, event: SessionEvent) -> Recorded | None:
        """Keep the event and answer a position."""
        self.recorded.append(event)
        return Recorded(seq=len(self.recorded), at=datetime.now(UTC), event=event)

    def events(self) -> tuple[Recorded, ...]:
        """Everything recorded so far."""
        return tuple(
            Recorded(seq=index, at=datetime.now(UTC), event=event) for index, event in enumerate(self.recorded, start=1)
        )

    def boot_sweep(self) -> None:
        """Do nothing: a fake log has no torn tail to close."""
