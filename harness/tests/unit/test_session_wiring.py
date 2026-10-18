"""The session log meets the runtime: the boot sweep, the migrated marker, and the Forget boundary.

Every case opens its own log under its own state home, so nothing here reads or writes the
deployment's own timeline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from harness.adapters.session_log import SessionLog
from harness.domain.events import ConversationForgotten, TurnAbandoned, TurnSettled, TurnStarted, last_forgotten_at, open_turns
from tests.support.state import a_state

if TYPE_CHECKING:
    from pathlib import Path


def _log(home: Path) -> SessionLog:
    """One session log under `home`, with the forget marker it sweeps beside it."""
    state = a_state(home)
    return SessionLog(state.session_log, forgotten_marker=state.forgotten_marker, topics=None)


def _kinds(log: SessionLog) -> list[str]:
    return [record.event.kind for record in log.events()]


class TestBootSweep:
    def test_an_open_turn_is_closed_as_abandoned(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        started = log.record(TurnStarted(entry="spa", chars=5))
        assert started is not None

        log.boot_sweep()

        assert _kinds(log) == ["turn_started", "turn_abandoned"]
        abandoned = log.events()[-1].event
        assert isinstance(abandoned, TurnAbandoned)
        assert abandoned.started_seq == started.seq

    def test_a_clean_journal_is_left_alone(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.record(TurnStarted(entry="spa", chars=5))
        log.record(TurnSettled(run_id=None))

        log.boot_sweep()

        assert _kinds(log) == ["turn_started", "turn_settled"]
        assert open_turns(log.events()) == ()

    def test_a_forget_marker_migrates_into_a_boundary_event_and_is_deleted(self, tmp_path: Path) -> None:
        stamp = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
        marker = a_state(tmp_path).forgotten_marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(stamp.isoformat(), encoding="utf-8")
        log = _log(tmp_path)

        log.boot_sweep()

        assert not marker.exists()
        assert last_forgotten_at(log.events()) == stamp

    def test_an_unreadable_marker_is_dropped_not_fatal(self, tmp_path: Path) -> None:
        marker = a_state(tmp_path).forgotten_marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("not a timestamp", encoding="utf-8")
        log = _log(tmp_path)

        log.boot_sweep()

        assert not marker.exists()
        assert last_forgotten_at(log.events()) is None
        assert _kinds(log) == []


class TestForgetBoundary:
    def test_the_journal_boundary_is_the_watermark_resume_honors(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.record(ConversationForgotten())

        watermark = last_forgotten_at(log.events())

        assert watermark is not None
