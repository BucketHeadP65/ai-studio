"""Two session logs appending to one file mint every sequence number once, in order."""

from __future__ import annotations

import threading

from typing import TYPE_CHECKING

from harness.adapters.session_log import SessionLog
from harness.domain.contracts import TurnStarted

if TYPE_CHECKING:
    from pathlib import Path


def test_two_logs_over_one_file_never_mint_the_same_sequence_number(tmp_path: Path) -> None:
    path = tmp_path / "session-log.jsonl"
    logs = (SessionLog(path, topics=None), SessionLog(path, topics=None))

    def append(log: SessionLog) -> None:
        for _ in range(40):
            log.record(TurnStarted(entry="spa", chars=1, session_id="s1", prompt_excerpt=None))

    threads = [threading.Thread(target=append, args=(log,)) for log in logs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [record.seq for record in logs[0].events()] == list(range(1, 81))


def test_a_torn_last_line_does_not_stop_the_count(tmp_path: Path) -> None:
    path = tmp_path / "session-log.jsonl"
    log = SessionLog(path, topics=None)
    log.record(TurnStarted(entry="spa", chars=1, session_id="s1", prompt_excerpt=None))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "torn')

    recorded = log.record(TurnStarted(entry="spa", chars=1, session_id="s1", prompt_excerpt=None))

    assert recorded is not None
    assert recorded.seq == 2
