"""`harness sessions` -- the owner's window onto the session log: list, tail, search.

Every case points the console verb's own state home at its own directory, the way a deployment
points it: the session log is written through the real adapter, so what the commands print is what
the file holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from harness.adapters.store import RecordDatabase, SessionsDatabase
from harness.adapters.wiring import RecordPoolOpener
from harness.cli import app, provision_record
from harness.container import create_container
from harness.domain.events import Booted, ConversationForgotten, SessionRenamed, SessionStarted, TurnSettled, TurnStarted
from harness.settings import HarnessSettings
from tests.support.container import rooted_at
from tests.support.fakes.pools import FakePool, FakeRecordPool, FakeSessionsDatabase
from tests.support.state import a_session_log, a_state

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _seed_conversations(tmp_path: Path) -> None:
    a_session_log(tmp_path).record(Booted(resumed_run_id=None))
    a_session_log(tmp_path).record(SessionStarted(session_id="a", session_kind="chat"))
    a_session_log(tmp_path).record(TurnStarted(entry="spa", chars=12, session_id="a", prompt_excerpt=None))
    a_session_log(tmp_path).record(TurnSettled(run_id="harness-author-abc123", started_seq=3))
    a_session_log(tmp_path).record(ConversationForgotten(session_id="a"))
    a_session_log(tmp_path).record(TurnStarted(entry="spa", chars=7, session_id="b", prompt_excerpt=None))


def test_list_shows_each_conversation_span_with_its_turn_count(tmp_path: Path) -> None:
    _seed_conversations(tmp_path)

    result = runner.invoke(app, ["sessions", "list"], env={"APP_STATE__HOME": str(tmp_path)})

    assert result.exit_code == 0
    assert "1 turn" in result.output
    assert "live" in result.output


def test_list_shows_the_sessions_own_id_and_title(tmp_path: Path) -> None:
    session_id = "11111111-1111-1111-1111-111111111111"
    a_session_log(tmp_path).record(SessionStarted(session_id=session_id, session_kind="chat"))
    a_session_log(tmp_path).record(TurnStarted(entry="spa", chars=4, session_id=session_id, prompt_excerpt=None))
    a_session_log(tmp_path).record(TurnSettled(run_id="run-1", started_seq=2))
    a_session_log(tmp_path).record(SessionRenamed(session_id=session_id, title="Quarterly Ledger Review"))

    result = runner.invoke(app, ["sessions", "list"], env={"APP_STATE__HOME": str(tmp_path)})

    assert result.exit_code == 0
    assert session_id[:8] in result.output
    assert "Quarterly Ledger Review" in result.output


def test_list_with_an_empty_journal_says_so(tmp_path: Path) -> None:
    result = runner.invoke(app, ["sessions", "list"], env={"APP_STATE__HOME": str(tmp_path)})

    assert result.exit_code == 0
    assert "no sessions" in result.output


def test_tail_prints_the_newest_events_with_their_seq(tmp_path: Path) -> None:
    _seed_conversations(tmp_path)

    result = runner.invoke(app, ["sessions", "tail", "--n", "2"], env={"APP_STATE__HOME": str(tmp_path)})

    assert result.exit_code == 0
    assert "conversation_forgotten" in result.output
    assert "turn_started" in result.output
    assert "boot" not in result.output


def test_search_finds_a_term_in_the_session_log_and_the_state_home(tmp_path: Path) -> None:
    _seed_conversations(tmp_path)
    kept = a_state(tmp_path).home / "harness-author-abc123.json"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text('{"parts": "the quarterly ledger balances"}', encoding="utf-8")

    result = runner.invoke(app, ["sessions", "search", "quarterly"], env={"APP_STATE__HOME": str(tmp_path)})

    assert result.exit_code == 0
    assert "harness-author-abc123" in result.output


def test_search_says_when_nothing_matches(tmp_path: Path) -> None:
    _seed_conversations(tmp_path)

    result = runner.invoke(app, ["sessions", "search", "zanzibar"], env={"APP_STATE__HOME": str(tmp_path)})

    assert result.exit_code == 0
    assert "no matches" in result.output


async def test_rebuild_takes_the_records_home_from_the_container(tmp_path: Path) -> None:
    """The record `sessions rebuild` provisions is the container's, tables and all, not a pool of its own."""
    record = FakePool()
    container = create_container(rooted_at(HarnessSettings(), tmp_path))
    with container.override({SessionsDatabase: FakeSessionsDatabase(), RecordPoolOpener: FakeRecordPool(record)}):
        await provision_record(container)
        assert isinstance(await container.get(RecordDatabase), RecordDatabase)

    assert any("CREATE TABLE IF NOT EXISTS runs" in statement for statement, _ in record.statements)
    await container.close()


def test_sessions_has_no_import_verb(tmp_path: Path) -> None:
    """Every session lives in the record. There is no transcript folder left to copy into it."""
    result = runner.invoke(app, ["sessions", "import"], env={"APP_STATE__HOME": str(tmp_path)})

    assert result.exit_code == 2
