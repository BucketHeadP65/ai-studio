"""The append-only session log file: monotonic seq across restarts, torn tails skipped, no globals.

Every test builds its own `SessionLog` on a `tmp_path` file with a fixed clock — path and
clock are constructor arguments, so nothing here patches anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from harness.adapters.session_log import SessionLog
from harness.domain.events import ConversationForgotten, TurnStarted, decode
from tests.support.state import recorded

if TYPE_CHECKING:
    from pathlib import Path

_NOON = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _journal(path: Path) -> SessionLog:
    return SessionLog(path, clock=lambda: _NOON, topics=None)


def test_recording_assigns_monotonic_seq(tmp_path: Path) -> None:
    session_log = _journal(tmp_path / "session-log.jsonl")

    first = recorded(session_log, TurnStarted(entry="spa", chars=5))
    second = recorded(session_log, ConversationForgotten())

    assert (first.seq, second.seq) == (1, 2)
    assert first.at == _NOON


def test_seq_continues_across_instances(tmp_path: Path) -> None:
    """A restart keeps counting: the next process reads where the last one stopped."""
    path = tmp_path / "session-log.jsonl"
    _journal(path).record(TurnStarted(entry="spa", chars=5))

    resumed = recorded(_journal(path), ConversationForgotten())

    assert resumed.seq == 2


def test_events_round_trip(tmp_path: Path) -> None:
    session_log = _journal(tmp_path / "session-log.jsonl")
    session_log.record(TurnStarted(entry="acp", chars=9))
    session_log.record(ConversationForgotten())

    events = session_log.events()

    assert [record.seq for record in events] == [1, 2]
    assert events[0].event == TurnStarted(entry="acp", chars=9)


def test_a_torn_tail_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A crash mid-append loses at most the final line; the reader shrugs and the writer moves on."""
    path = tmp_path / "session-log.jsonl"
    session_log = _journal(path)
    whole = recorded(session_log, TurnStarted(entry="spa", chars=5))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "at": "2026-08-2')

    assert [record.seq for record in session_log.events()] == [whole.seq]
    assert recorded(session_log, ConversationForgotten()).seq == 2


def test_a_missing_file_reads_as_empty(tmp_path: Path) -> None:
    assert _journal(tmp_path / "never-written.jsonl").events() == ()


def test_lines_are_plain_jsonl(tmp_path: Path) -> None:
    """One event per line, flat keys — the file stays greppable without the codec."""
    path = tmp_path / "session-log.jsonl"
    _journal(path).record(TurnStarted(entry="spa", chars=5))

    (line,) = path.read_text(encoding="utf-8").splitlines()

    assert decode(line) is not None
    assert '"kind": "turn_started"' in line or '"kind":"turn_started"' in line
