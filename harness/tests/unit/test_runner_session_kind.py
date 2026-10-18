"""Runner sessions as a first-class kind: the event field's decode default.

A legacy log entry carries no `session_kind` at all and must still decode, defaulted to
`chat`. The projected column and the HTTP filter are exercised against a real Postgres in
`tests/integration/test_runner_session_kind.py`.
"""

from __future__ import annotations

from harness.domain.events import SessionStarted, decode


def test_a_legacy_session_started_line_with_no_session_kind_decodes_as_chat() -> None:
    line = (
        '{"seq": 1, "at": "2026-08-22T12:00:00+00:00", "kind": "session_started", '
        '"session_id": "11111111-1111-1111-1111-111111111111"}'
    )

    record = decode(line)

    assert record is not None
    assert isinstance(record.event, SessionStarted)
    assert record.event.session_kind == "chat"
