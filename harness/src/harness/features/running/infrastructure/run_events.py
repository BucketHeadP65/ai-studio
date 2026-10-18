"""The run's own event file: one append-only JSONL per run, beside the run's other state.

Each fact is appended as it lands, such as a pause, a resume or a gate reached, and the file
outlives the run. The event grammar and its codec come from `core.ai.workflows`. The module's
`record` never raises, so recording an event can never be why a run dies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import RecordedRunEvent, decode_run_event, encode_run_event
from core.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.ai.workflows.contracts import RunEvent

    from harness.settings import StateSettings

_logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


class RunEventLog:
    """One run's event file."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _now) -> None:
        """An event log over the file at `path`, which the first append creates.

        Args:
            path: The run's event file.
            clock: What stamps each recorded event. It defaults to the current UTC time, to the second.
        """
        self._path = path
        self._clock = clock

    def record(self, event: RunEvent) -> RecordedRunEvent:
        """Append one event under the next sequence number.

        Args:
            event: The event to append.

        Returns:
            The entry as recorded, with its sequence number and its time.

        Raises:
            OSError: The file could not be written.
        """
        recorded = RecordedRunEvent(seq=self._next_seq(), at=self._clock(), event=event)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(encode_run_event(recorded) + "\n")
        return recorded

    def events(self) -> tuple[RecordedRunEvent, ...]:
        """Every readable entry, in order.

        A torn line or an unknown event type costs that one line, never the read.

        Returns:
            The entries, empty when the file is missing or cannot be read.
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return ()
        return tuple(entry for line in text.splitlines() if (entry := decode_run_event(line)) is not None)

    def _next_seq(self) -> int:
        """One past the newest readable sequence number, read from the file on every append."""
        entries = self.events()
        return entries[-1].seq + 1 if entries else 1


def record(state: StateSettings, event: RunEvent) -> RecordedRunEvent | None:
    """Append `event` to the file of the run it names, under `state`'s home.

    It never raises.

    Args:
        state: The state settings whose home holds every run's event file.
        event: The event. One that names no run is not recorded.

    Returns:
        The entry as recorded, or `None` when the disk refused or the event names no run.
    """
    if not event.run_id:
        return None
    try:
        return RunEventLog(state.run_events(event.run_id)).record(event)
    except OSError:
        _logger.warning("run log write failed for {kind}", kind=event.kind)
        return None


def events_for(state: StateSettings, run_id: str) -> tuple[RecordedRunEvent, ...]:
    """Everything recorded about `run_id` under `state`'s home, in order.

    Args:
        state: The state settings whose home holds every run's event file.
        run_id: The run to read.

    Returns:
        The entries, empty when nothing was ever recorded.
    """
    return RunEventLog(state.run_events(run_id)).events()
