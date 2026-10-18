"""`harness sessions friction`: the flags agents filed, newest first, straight off the session log."""

from __future__ import annotations

from datetime import UTC, datetime

from harness.domain.events import FrictionFlagged, FrictionTargetType, Recorded, SessionStarted
from harness.features.sessions.flags import friction_lines


def _flag(seq: int, *, target_kind: FrictionTargetType, target: str, note: str, session: str | None) -> Recorded:
    event = FrictionFlagged(target_kind=target_kind, target=target, note=note, tool_call_id=None, source="live", subject_session_id=session)
    return Recorded(seq=seq, at=datetime(2026, 8, 27, 12, seq, tzinfo=UTC), event=event)


class TestFrictionLines:
    def test_newest_first_with_kind_target_session_and_note(self):
        records = (
            _flag(1, target_kind="tool", target="shell", note="exit codes confusing", session="11111111-aaaa"),
            _flag(2, target_kind="skill", target="planning", note="too long", session="22222222-bbbb"),
        )

        lines = friction_lines(records)

        assert lines[0].startswith("2026-08-27T12:02")
        assert "skill" in lines[0]
        assert "planning" in lines[0]
        assert "22222222" in lines[0]
        assert "too long" in lines[0]
        assert "shell" in lines[1]

    def test_other_events_are_ignored(self):
        started = Recorded(seq=1, at=datetime(2026, 8, 27, 12, 1, tzinfo=UTC), event=SessionStarted(session_id="11111111-aaaa"))

        assert friction_lines((started,)) == []
        assert len(friction_lines((started, _flag(2, target_kind="tool", target="shell", note="x", session=None)))) == 1

    def test_a_target_kind_filter_keeps_only_that_kind(self):
        records = (
            _flag(1, target_kind="tool", target="shell", note="a", session=None),
            _flag(2, target_kind="skill", target="planning", note="b", session=None),
        )

        lines = friction_lines(records, target_kind="skill")

        assert len(lines) == 1
        assert "planning" in lines[0]
