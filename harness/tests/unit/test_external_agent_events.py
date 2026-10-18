"""The three external-agent events: in the union, and round-tripping through the codec.

An event a newer process wrote must decode here, and one this process writes must decode after a
restart, so the codec round trip is the contract worth pinning.
"""

from __future__ import annotations

from datetime import UTC, datetime

from harness.domain.events import ExternalAgentFinished, ExternalAgentStarted, ExternalAgentUpdated, Recorded, decode, encode

_T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def test_a_start_round_trips() -> None:
    record = Recorded(seq=1, at=_T0, event=ExternalAgentStarted(agent="codex", cwd="/work/project"))

    back = decode(encode(record))

    assert back is not None
    assert back == record
    assert isinstance(back.event, ExternalAgentStarted)
    assert back.event.kind == "external_agent_started"


def test_an_update_round_trips() -> None:
    record = Recorded(seq=2, at=_T0, event=ExternalAgentUpdated(agent="codex", text="reading src/main.py"))

    back = decode(encode(record))

    assert back == record


def test_a_finish_round_trips_with_its_touched_files() -> None:
    record = Recorded(
        seq=3,
        at=_T0,
        event=ExternalAgentFinished(agent="codex", stop_reason="end_turn", touched_files=("a.py", "b.py")),
    )

    back = decode(encode(record))

    assert back is not None
    assert back == record
    assert isinstance(back.event, ExternalAgentFinished)
    assert back.event.touched_files == ("a.py", "b.py")


def test_the_encoded_line_is_flat_and_greppable() -> None:
    line = encode(Recorded(seq=4, at=_T0, event=ExternalAgentStarted(agent="pi", cwd="/w")))

    assert '"kind": "external_agent_started"' in line
    assert '"agent": "pi"' in line
