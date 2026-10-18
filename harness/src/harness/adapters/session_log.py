"""The session log on disk: one append-only JSONL file, owned by whoever opened it.

This module numbers each event, appends it as one line and reads back every line it can parse.
The event types, their encoding and the projections over them live in `harness.domain.events`.
A write failure logs a warning and never fails the turn it records.
"""

from __future__ import annotations

import os

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.files import exclusive_lock
from core.observability import get_logger

from harness.domain.contracts import (
    ConversationForgotten,
    Recorded,
    SessionArchived,
    SessionDeleted,
    SessionRenamed,
    SessionStarted,
    TurnAbandoned,
    TurnStarted,
    decode,
    encode,
    open_turns,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.cqrs.contracts import TopicSink

    from harness.domain.contracts import SessionEvent

_logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


def _session_of(event: SessionEvent) -> str | None:
    """The session `event` belongs to, when its own type names one."""
    match event:
        case (
            SessionStarted(session_id=session_id)
            | SessionRenamed(session_id=session_id)
            | SessionArchived(session_id=session_id)
            | SessionDeleted(session_id=session_id)
            | TurnStarted(session_id=session_id)
            | ConversationForgotten(session_id=session_id)
        ):
            return session_id
        case _:
            return None


_TAIL_BYTES = 65_536
"""How much of the log's end is read to find the newest sequence number before the whole log is read."""

class SessionLog:
    """One session log file, appended to by `record` and read back by `events`."""

    def __init__(
        self,
        path: Path,
        *,
        forgotten_marker: Path | None = None,
        clock: Callable[[], datetime] = _now,
        topics: TopicSink | None,
    ) -> None:
        """Use the log file at `path`, touching the disk only when a method reads or appends.

        Args:
            path: The JSONL file. Its folder is created on the first append.
            forgotten_marker: The `forgotten-at` marker file an older process may have left, which
                `boot_sweep` turns into an event. `None` skips that step.
            clock: What stamps each event. Defaults to the UTC time, to the second.
            topics: Where each append is announced, or `None` to announce nothing.
        """
        self._path = path
        self._forgotten_marker = forgotten_marker
        self._clock = clock
        self._topics = topics

    def record(self, event: SessionEvent, *, at: datetime | None = None) -> Recorded | None:
        """Append one event with the next sequence number, announce the change, and return the entry as written.

        The number is minted under the log's lock, so two processes appending to one log never mint the same one.
        A disk failure never raises. It logs a warning and returns `None`.

        Args:
            event: The event to append.
            at: The time to stamp in place of the clock's, for an act that happened before this process started.

        Returns:
            The entry as written, or `None` when the disk refused the write.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with exclusive_lock(self._path.with_name(f"{self._path.name}.lock")):
                recorded = Recorded(seq=self._next_seq(), at=at if at is not None else self._clock(), event=event)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(encode(recorded) + "\n")
        except OSError:
            _logger.warning("session log write failed for {kind}", kind=event.kind)
            return None
        self._announce(event)
        return recorded

    def boot_sweep(self) -> None:
        """Bring the log up to date before the first request.

        First, a `forgotten-at` marker an older process left becomes a forgotten boundary event stamped
        with the marker's own time, and the marker is deleted. An unreadable marker is dropped with a warning.
        Then every turn that started and never ended is closed as abandoned.
        Call it before `resume`, so resume honours the migrated boundary. It records no `Booted` event,
        which the caller records once it knows what it resumed.
        A log opened with no marker path only closes the open turns.
        """
        marker = self._forgotten_marker
        if marker is not None and marker.exists():
            try:
                stamp = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
            except ValueError:
                _logger.warning("dropping an unreadable forgotten-at marker")
            else:
                self.record(ConversationForgotten(), at=stamp)
            marker.unlink(missing_ok=True)
        for open_turn in open_turns(self.events()):
            self.record(TurnAbandoned(started_seq=open_turn.seq))

    def _announce(self, event: SessionEvent) -> None:
        """Announce that the session list changed, and which session when the event names one, unless there is no channel."""
        if self._topics is None:
            return
        self._topics.publish("sessions")
        session_id = _session_of(event)
        if session_id:
            self._topics.publish(f"session.{session_id}")

    def events(self) -> tuple[Recorded, ...]:
        """Every readable entry, in order.

        A torn last line or an unknown event type costs that one line, never the whole read.

        Returns:
            The entries in sequence order, empty when the file is missing or cannot be read.
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return ()
        return tuple(record for line in text.splitlines() if (record := decode(line)) is not None)

    def _next_seq(self) -> int:
        """One past the newest readable sequence number, read from the log's tail so a restart keeps counting."""
        try:
            with self._path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(size - _TAIL_BYTES, 0))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return 1
        for line in reversed(tail.splitlines()):
            if (record := decode(line)) is not None:
                return record.seq + 1
        events = self.events()
        return events[-1].seq + 1 if events else 1
