"""The session-event grammar and its projections: pure, closed, and forgiving at the edges.

The codec round-trips every kind; a torn or unrecognised line decodes to `None` (the reader
skips it, the same forgiving-boundary rule as boot-time resume); the projections answer the
three questions the runtime asks the log: which turns never ended, when the person last said
Forget, and what the conversation spans are.
"""

from __future__ import annotations

import uuid

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from harness.domain.events import (
    _LEGACY_SESSION_NS,
    Booted,
    ConversationForgotten,
    DraftSettled,
    FrictionFlagged,
    PluginSet,
    ProfileSelected,
    Recorded,
    RunLaunched,
    SessionArchived,
    SessionDeleted,
    SessionRenamed,
    SessionStarted,
    TierSelected,
    TurnAbandoned,
    TurnFailed,
    TurnSettled,
    TurnStarted,
    decode,
    encode,
    last_forgotten_at,
    live_session,
    open_turns,
    session_spans,
)

if TYPE_CHECKING:
    from harness.domain.events import SessionEvent

_T0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
_NOON = _T0


def _rec(seq: int, event: SessionEvent) -> Recorded:
    return Recorded(seq=seq, at=_T0.replace(minute=seq), event=event)


def test_new_kinds_roundtrip() -> None:
    flagged = FrictionFlagged(
        target_kind="tool", target="run_command", note="timeout unclear", tool_call_id="call_1", source="live", subject_session_id=None
    )
    for event in (
        SessionStarted(session_id="s1"),
        SessionRenamed(session_id="s1", title="t"),
        SessionArchived(session_id="s1", archived=True),
        SessionDeleted(session_id="s1"),
        flagged,
    ):
        line = encode(Recorded(seq=1, at=_NOON, event=event))
        decoded = decode(line)
        assert decoded is not None
        assert decoded.event == event


def test_old_lines_still_decode() -> None:
    line = '{"seq": 3, "at": "2026-08-21T10:00:00+00:00", "kind": "conversation_forgotten"}'
    decoded = decode(line)
    assert decoded is not None
    assert isinstance(decoded.event, ConversationForgotten)


def test_a_retro_sourced_flag_line_still_decodes() -> None:
    line = (
        '{"seq": 29, "at": "2026-08-23T05:29:14+00:00", "kind": "friction_flagged", '
        '"target_kind": "workflow", "target": "tool-result-handles/read_tool_result workflow", '
        '"note": "Large tool outputs were repeatedly returned only as opaque handles.", '
        '"tool_call_id": null, "source": "retro", '
        '"subject_session_id": "c9acd653-b4c7-44a8-8d8f-6cb05d8e932f"}'
    )
    decoded = decode(line)
    assert decoded is not None
    assert isinstance(decoded.event, FrictionFlagged)
    assert decoded.event.source == "retro"


def test_spans_carry_explicit_and_legacy_ids() -> None:
    events = (
        _rec(1, TurnStarted(entry="spa", chars=3)),
        _rec(2, TurnSettled(run_id="r1")),  # legacy span
        _rec(3, ConversationForgotten()),
        _rec(4, SessionStarted(session_id="s2")),
        _rec(5, TurnStarted(entry="spa", chars=3)),
        _rec(6, TurnSettled(run_id="r2")),
    )
    first, second = session_spans(events)
    assert first.session_id == str(uuid.uuid5(_LEGACY_SESSION_NS, "1"))
    assert first.run_ids == ("r1",)
    assert second.session_id == "s2"
    assert second.ended_at is None


def test_live_session_is_the_newest_one_the_timeline_opened() -> None:
    events = (
        _rec(1, SessionStarted(session_id="a")),
        _rec(2, TurnStarted(entry="spa", chars=1)),
        _rec(3, TurnSettled(run_id="ra")),
        _rec(4, ConversationForgotten()),
        _rec(5, SessionStarted(session_id="b")),
        _rec(6, TurnStarted(entry="spa", chars=1)),
        _rec(7, TurnSettled(run_id="rb")),
    )
    live = live_session(events)
    assert live is not None
    assert live.session_id == "b"
    assert live.last_run_id == "rb"
    assert live_session(events[:4]) is None


class TestCodec:
    def test_every_kind_round_trips(self) -> None:
        events = [
            Booted(resumed_run_id="harness-author-abc123"),
            Booted(resumed_run_id=None),
            TurnStarted(entry="spa", chars=42),
            TurnSettled(run_id="harness-author-abc123"),
            TurnFailed(error="the gateway refused the key"),
            TurnAbandoned(started_seq=7),
            ConversationForgotten(),
            DraftSettled(name="quarterly-report", version=3),
            PluginSet(plugin="sql", enabled=True),
            ProfileSelected(name="researcher"),
            ProfileSelected(name=None),
            TierSelected(tier="worker", model="gpt-5.4", thinking="low"),
            TierSelected(tier="worker", model=None, thinking=None),
            RunLaunched(workflow_id="wf.quarterly-report.v3", run_id="20260903T120000-abcd1234"),
        ]
        for seq, event in enumerate(events, start=1):
            line = encode(_rec(seq, event))

            decoded = decode(line)

            assert decoded == _rec(seq, event)

    def test_a_torn_line_decodes_to_none(self) -> None:
        whole = encode(_rec(1, ConversationForgotten()))

        assert decode(whole[: len(whole) // 2]) is None

    def test_an_unknown_kind_decodes_to_none(self) -> None:
        assert decode('{"seq": 9, "at": "2026-08-22T12:00:00+00:00", "kind": "from_the_future"}') is None

    def test_a_line_missing_the_envelope_decodes_to_none(self) -> None:
        assert decode('{"kind": "conversation_forgotten"}') is None


def test_turn_settled_carries_the_calls_it_stopped_for_and_roundtrips() -> None:
    record = _rec(1, TurnSettled(run_id="run-1", started_seq=0, pending=("navigate", "toggle_plugin")))
    decoded = decode(encode(record))
    assert decoded is not None
    assert isinstance(decoded.event, TurnSettled)
    assert decoded.event.pending == ("navigate", "toggle_plugin")
    assert TurnSettled(run_id="run-1").pending == ()


class TestStampedFolds:
    """The four scenarios that motivate the stamps: interleaving, legacy, pairing, and closing."""

    def test_interleaved_stamped_turn_brackets_fold_into_two_spans(self) -> None:
        events = (
            _rec(1, TurnStarted(entry="spa", chars=3, session_id="a")),
            _rec(2, TurnStarted(entry="spa", chars=4, session_id="b")),
            _rec(3, TurnSettled(run_id="ra", started_seq=1)),
            _rec(4, TurnSettled(run_id="rb", started_seq=2)),
        )

        spans = session_spans(events)

        by_id = {span.session_id: span for span in spans}
        assert set(by_id) == {"a", "b"}
        assert by_id["a"].run_ids == ("ra",)
        assert by_id["b"].run_ids == ("rb",)
        assert by_id["a"].turns == 1
        assert by_id["b"].turns == 1

    def test_an_unstamped_legacy_journal_folds_exactly_as_today(self) -> None:
        events = (
            _rec(1, TurnStarted(entry="spa", chars=3)),
            _rec(2, TurnSettled(run_id="r1")),
            _rec(3, ConversationForgotten()),
            _rec(4, SessionStarted(session_id="s2")),
            _rec(5, TurnStarted(entry="spa", chars=3)),
            _rec(6, TurnSettled(run_id="r2")),
        )

        spans = session_spans(events)

        assert len(spans) == 2
        assert spans[0].session_id == str(uuid.uuid5(_LEGACY_SESSION_NS, "1"))
        assert spans[0].run_ids == ("r1",)
        assert spans[1].session_id == "s2"
        assert spans[1].run_ids == ("r2",)
        assert spans[1].ended_at is None

    def test_open_turns_pairs_a_stamped_settle_to_its_own_start(self) -> None:
        events = (
            _rec(1, TurnStarted(entry="spa", chars=1, session_id="a")),
            _rec(2, TurnStarted(entry="spa", chars=1, session_id="b")),
            _rec(3, TurnSettled(run_id="ra", started_seq=1)),
        )

        assert [record.seq for record in open_turns(events)] == [2]

    def test_a_stamped_conversation_forgotten_closes_only_its_own_session(self) -> None:
        events = (
            _rec(1, TurnStarted(entry="spa", chars=1, session_id="a")),
            _rec(2, TurnStarted(entry="spa", chars=1, session_id="b")),
            _rec(3, TurnSettled(run_id="ra", started_seq=1)),
            _rec(4, ConversationForgotten(session_id="a")),
            _rec(5, TurnSettled(run_id="rb", started_seq=2)),
        )

        spans = session_spans(events)

        by_id = {span.session_id: span for span in spans}
        assert by_id["a"].ended_at == _T0.replace(minute=4)
        assert by_id["a"].run_ids == ("ra",)
        assert by_id["b"].ended_at is None
        assert by_id["b"].run_ids == ("rb",)


class TestOpenTurns:
    def test_a_settled_turn_is_not_open(self) -> None:
        events = (_rec(1, TurnStarted(entry="spa", chars=5)), _rec(2, TurnSettled(run_id=None)))

        assert open_turns(events) == ()

    def test_a_started_turn_with_no_terminal_event_is_open(self) -> None:
        events = (_rec(1, TurnStarted(entry="spa", chars=5)),)

        assert [record.seq for record in open_turns(events)] == [1]

    def test_a_failed_turn_is_not_open(self) -> None:
        events = (_rec(1, TurnStarted(entry="acp", chars=5)), _rec(2, TurnFailed(error="boom")))

        assert open_turns(events) == ()

    def test_an_abandonment_closes_exactly_the_turn_it_names(self) -> None:
        events = (
            _rec(1, TurnStarted(entry="spa", chars=5)),
            _rec(2, TurnAbandoned(started_seq=1)),
            _rec(3, TurnStarted(entry="spa", chars=6)),
        )

        assert [record.seq for record in open_turns(events)] == [3]

    def test_a_terminal_event_closes_the_latest_open_turn(self) -> None:
        # Turns are sequential in this app; if the log ever carries two open starts, the settle
        # binds to the newest and the older one stays visibly open for the boot sweep to close.
        events = (_rec(1, TurnStarted(entry="spa", chars=5)), _rec(2, TurnStarted(entry="spa", chars=6)), _rec(3, TurnSettled(run_id=None)))

        assert [record.seq for record in open_turns(events)] == [1]


class TestForgottenAt:
    def test_no_boundary_means_none(self) -> None:
        assert last_forgotten_at((_rec(1, TurnStarted(entry="spa", chars=5)),)) is None

    def test_the_newest_boundary_wins(self) -> None:
        events = (_rec(1, ConversationForgotten()), _rec(4, ConversationForgotten()))

        assert last_forgotten_at(events) == _T0.replace(minute=4)

