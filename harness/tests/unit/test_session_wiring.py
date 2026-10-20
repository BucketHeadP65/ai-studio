"""The session log meets the runtime: the boot sweep and the Forget boundary.

Every case opens its own log under its own state home, so nothing here reads or writes the
deployment's own timeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.adapters.session_log import SessionLog
from harness.domain.events import ConversationForgotten, TurnAbandoned, TurnSettled, TurnStarted, last_forgotten_at, open_turns
from tests.support.state import a_state

if TYPE_CHECKING:
    from pathlib import Path


def _log(home: Path) -> SessionLog:
    """One session log under `home`."""
    return SessionLog(a_state(home).session_log, topics=None)


def _kinds(log: SessionLog) -> list[str]:
    return [record.event.kind for record in log.events()]


class TestBootSweep:
    def test_an_open_turn_is_closed_as_abandoned(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        started = log.record(TurnStarted(entry="spa", chars=5, session_id="s1", prompt_excerpt=None))
        assert started is not None

        log.boot_sweep()

        assert _kinds(log) == ["turn_started", "turn_abandoned"]
        abandoned = log.events()[-1].event
        assert isinstance(abandoned, TurnAbandoned)
        assert abandoned.started_seq == started.seq

    def test_a_clean_journal_is_left_alone(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.record(TurnStarted(entry="spa", chars=5, session_id="s1", prompt_excerpt=None))
        log.record(TurnSettled(run_id=None, started_seq=1))

        log.boot_sweep()

        assert _kinds(log) == ["turn_started", "turn_settled"]
        assert open_turns(log.events()) == ()


class TestForgetBoundary:
    def test_the_journal_boundary_is_the_watermark_resume_honors(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.record(ConversationForgotten(session_id=None))

        watermark = last_forgotten_at(log.events())

        assert watermark is not None
