"""The session projection fold against a real Postgres: rebuild, refresh, messages, usage.

Nothing here is mocked: a pass means the fold actually walked a real `SessionLog` file and a
real transcripts, and the rows an assertion reads came back from the server. `rebuild` truncates
the five tables on every call, so tests share one Postgres pool per test without stepping on
each other's data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, InMemoryStepStore
from typer.testing import CliRunner

from harness.adapters.session_log import SessionLog
from harness.domain.events import (
    ConversationForgotten,
    FrictionFlagged,
    Recorded,
    SessionArchived,
    SessionDeleted,
    SessionRenamed,
    SessionSpan,
    SessionStarted,
    TurnAbandoned,
    TurnFailed,
    TurnSettled,
    TurnStarted,
    session_spans,
)
from harness.features.sessions import project_registry, service
from harness.features.sessions.infrastructure.postgres import PostgresProjectionPool
from harness.features.sessions.infrastructure.projects_log import ProjectsLog
from harness.features.sessions.infrastructure.schema import ensure_schema
from harness.features.sessions.infrastructure.writes import delete_session_rows
from harness.features.sessions.service import rebuild, refresh_live, title_for
from harness.workspace import open_workspace
from tests.integration.conftest import as_asyncpg_dsn
from tests.support.state import recorded

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import pytest

    from pydantic_ai_harness.step_persistence import RunRecord, StepEvent, StepStore, ToolEffectRecord

    from harness.features.sessions.internal_contracts import ProjectionPool

SESSION_A = "11111111-1111-1111-1111-111111111111"
SESSION_B = "22222222-2222-2222-2222-222222222222"
_NOON = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

runner = CliRunner()


def _clock() -> datetime:
    return _NOON


class _Fixture:
    """One seeded session log + transcripts: session A carries the cumulative-snapshot shape."""

    def __init__(self, tmp_path: Path) -> None:
        session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
        session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
        session_log.record(TurnStarted(entry="spa", chars=5, prompt_excerpt=None, session_id=SESSION_A))  # turn 1: settles with run-1
        session_log.record(TurnSettled(run_id="run-1", started_seq=2))
        session_log.record(TurnStarted(entry="spa", chars=7, prompt_excerpt=None, session_id=SESSION_A))  # turn 2: fails
        session_log.record(TurnFailed(error="boom", prompt_excerpt=None, started_seq=4))
        abandoned = recorded(session_log, TurnStarted(entry="spa", chars=9, prompt_excerpt=None, session_id=SESSION_A))  # turn 3: abandoned
        session_log.record(TurnAbandoned(started_seq=abandoned.seq))
        session_log.record(TurnStarted(entry="spa", chars=11, prompt_excerpt=None, session_id=SESSION_A))  # turn 4: settles with run-2 (cumulative)
        session_log.record(TurnSettled(run_id="run-2", started_seq=8))
        session_log.record(SessionRenamed(session_id=SESSION_A, title="Renamed Session Title"))
        session_log.record(SessionStarted(session_id=SESSION_B, session_kind="chat"))
        session_log.record(TurnStarted(entry="acp", chars=3, prompt_excerpt=None, session_id=SESSION_B))  # session B's one turn
        session_log.record(TurnSettled(run_id="run-b", started_seq=12))
        session_log.record(SessionDeleted(session_id=SESSION_B))
        self.events: tuple[Recorded, ...] = session_log.events()

        self.store = InMemoryStepStore()
        self.msg1 = ModelRequest(parts=[UserPromptPart(content="Hello there, how are you today?")])
        self.msg2 = ModelResponse(
            parts=[TextPart(content="Hi! Doing great.")],
            usage=RequestUsage(input_tokens=10, output_tokens=5, cache_read_tokens=1, cache_write_tokens=2),
        )
        self.msg3 = ModelRequest(parts=[UserPromptPart(content="Second question")])
        self.msg4 = ModelResponse(
            parts=[TextPart(content="Second answer")],
            usage=RequestUsage(input_tokens=20, output_tokens=8),
        )
        self.msg5 = ModelRequest(parts=[UserPromptPart(content="Session B question")])
        self.msg6 = ModelResponse(
            parts=[TextPart(content="Session B answer")],
            usage=RequestUsage(input_tokens=3, output_tokens=2),
        )

    async def seed_store(self) -> None:
        await self.store.save_snapshot(ContinuableSnapshot(run_id="run-1", step_index=0, messages=[self.msg1, self.msg2]))
        await self.store.save_snapshot(
            ContinuableSnapshot(run_id="run-2", step_index=0, messages=[self.msg1, self.msg2, self.msg3, self.msg4])
        )
        await self.store.save_snapshot(ContinuableSnapshot(run_id="run-b", step_index=0, messages=[self.msg5, self.msg6]))


async def _rebuilt(pool: ProjectionPool, tmp_path: Path) -> _Fixture:
    fixture = _Fixture(tmp_path)
    await fixture.seed_store()
    await ensure_schema(pool)
    await rebuild(pool, fixture.events, fixture.store, project_root=tmp_path)
    return fixture


async def test_schema_is_idempotent(projection_pool: ProjectionPool) -> None:
    await ensure_schema(projection_pool)
    await ensure_schema(projection_pool)
    tables = {r["tablename"] for r in await projection_pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"projects", "sessions", "turns", "messages", "flags"} <= tables


async def test_rebuild_writes_one_project_row(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    await _rebuilt(projection_pool, tmp_path)
    rows = await projection_pool.fetch("SELECT root_path, name FROM projects")
    assert len(rows) == 1
    assert rows[0]["root_path"] == str(tmp_path.resolve())
    assert rows[0]["name"] == tmp_path.resolve().name


async def test_rebuild_sessions_match_session_spans(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    fixture = await _rebuilt(projection_pool, tmp_path)
    spans = session_spans(fixture.events)
    span_a = next(span for span in spans if span.session_id == SESSION_A)

    row = await projection_pool.fetchrow("SELECT started_at, turn_count FROM sessions WHERE id = $1", SESSION_A)
    assert row is not None
    assert row["started_at"] == span_a.started_at
    assert row["turn_count"] == span_a.turns
    assert row["turn_count"] == 4


async def test_turns_outcome_honors_settle_fail_abandon(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    await _rebuilt(projection_pool, tmp_path)
    rows = await projection_pool.fetch(
        "SELECT seq, run_id, outcome, settled_at FROM turns WHERE session_id = $1 ORDER BY seq", SESSION_A
    )
    assert [r["outcome"] for r in rows] == ["settled", "failed", "abandoned", "settled"]
    assert [r["run_id"] for r in rows] == ["run-1", None, None, "run-2"]
    assert rows[0]["settled_at"] is not None
    assert rows[1]["settled_at"] is None
    assert rows[2]["settled_at"] is None
    assert rows[3]["settled_at"] is not None


async def test_messages_hold_only_each_runs_new_tail(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    await _rebuilt(projection_pool, tmp_path)
    run1_rows = await projection_pool.fetch("SELECT idx, kind, text_content FROM messages WHERE run_id = 'run-1' ORDER BY idx")
    run2_rows = await projection_pool.fetch("SELECT idx, kind, text_content FROM messages WHERE run_id = 'run-2' ORDER BY idx")

    assert [r["idx"] for r in run1_rows] == [0, 1]
    assert run1_rows[0]["kind"] == "request"
    assert "Hello there" in str(run1_rows[0]["text_content"])
    assert run1_rows[1]["kind"] == "response"
    assert "Doing great" in str(run1_rows[1]["text_content"])

    # run-2's snapshot is [msg1, msg2, msg3, msg4]; only the new tail (msg3, msg4) is stored here.
    assert [r["idx"] for r in run2_rows] == [0, 1]
    assert "Second question" in str(run2_rows[0]["text_content"])
    assert "Second answer" in str(run2_rows[1]["text_content"])
    assert "Hello there" not in str(run2_rows[0]["text_content"])


async def test_session_usage_sums_model_response_usage(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    await _rebuilt(projection_pool, tmp_path)
    row = await projection_pool.fetchrow(
        "SELECT input_tokens, output_tokens, cache_read_tokens, cache_write_tokens FROM sessions WHERE id = $1", SESSION_A
    )
    assert row is not None
    assert row["input_tokens"] == 30
    assert row["output_tokens"] == 13
    assert row["cache_read_tokens"] == 1
    assert row["cache_write_tokens"] == 2


async def test_session_renamed_seeds_the_title(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    await _rebuilt(projection_pool, tmp_path)
    row = await projection_pool.fetchrow("SELECT title FROM sessions WHERE id = $1", SESSION_A)
    assert row is not None
    assert row["title"] == "Renamed Session Title"


async def test_session_archived_latest_wins(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=4, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-archived", started_seq=2))
    session_log.record(SessionArchived(session_id=SESSION_A, archived=True))
    session_log.record(SessionArchived(session_id=SESSION_A, archived=False))
    events = session_log.events()
    store = InMemoryStepStore()

    await rebuild(projection_pool, events, store, project_root=tmp_path)

    row = await projection_pool.fetchrow("SELECT archived FROM sessions WHERE id = $1", SESSION_A)
    assert row is not None
    assert row["archived"] is False


async def test_session_deleted_leaves_no_rows(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    await _rebuilt(projection_pool, tmp_path)
    assert await projection_pool.fetchrow("SELECT 1 FROM sessions WHERE id = $1", SESSION_B) is None
    assert await projection_pool.fetchrow("SELECT 1 FROM turns WHERE session_id = $1", SESSION_B) is None
    assert await projection_pool.fetchrow("SELECT 1 FROM messages WHERE run_id = 'run-b'") is None


async def test_rebuild_twice_is_idempotent(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    fixture = _Fixture(tmp_path)
    await fixture.seed_store()

    await rebuild(projection_pool, fixture.events, fixture.store, project_root=tmp_path)
    counts_first = {
        table: await projection_pool.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608 -- table is one of a fixed local literal set
        for table in ("projects", "sessions", "turns", "messages", "flags")
    }

    await rebuild(projection_pool, fixture.events, fixture.store, project_root=tmp_path)
    counts_second = {
        table: await projection_pool.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608 -- same fixed local literal set
        for table in ("projects", "sessions", "turns", "messages", "flags")
    }

    assert counts_first == counts_second
    assert counts_first == {"projects": 1, "sessions": 1, "turns": 4, "messages": 4, "flags": 0}


async def test_title_for_falls_back_to_short_id_with_no_rename_or_excerpt() -> None:
    span = SessionSpan(session_id=SESSION_B, started_at=_NOON, ended_at=None, turns=0, run_ids=())
    events: tuple[Recorded, ...] = ()

    assert title_for(events, span) == f"session {SESSION_B[:8]}"


async def test_title_for_prefers_excerpt_over_short_id() -> None:
    span = SessionSpan(session_id=SESSION_B, started_at=_NOON, ended_at=None, turns=0, run_ids=())
    events: tuple[Recorded, ...] = ()

    assert title_for(events, span, excerpt="What is the answer") == "What is the answer"


async def test_refresh_live_upserts_only_the_live_session(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    await projection_pool.execute("TRUNCATE projects, sessions, turns, messages, flags")

    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=4, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-live", started_seq=2))
    events = session_log.events()

    store = InMemoryStepStore()
    request = ModelRequest(parts=[UserPromptPart(content="Live question")])
    response = ModelResponse(parts=[TextPart(content="Live answer")], usage=RequestUsage(input_tokens=4, output_tokens=2))
    await store.save_snapshot(ContinuableSnapshot(run_id="run-live", step_index=0, messages=[request, response]))

    await refresh_live(projection_pool, events, store, project_root=tmp_path)

    row = await projection_pool.fetchrow("SELECT id, turn_count, input_tokens FROM sessions WHERE id = $1", SESSION_A)
    assert row is not None
    assert row["turn_count"] == 1
    assert row["input_tokens"] == 4
    assert await projection_pool.fetchrow("SELECT 1 FROM sessions WHERE id = $1", SESSION_B) is None


async def test_refresh_live_upserts_the_named_session_not_the_timelines_newest(
    tmp_path: Path, projection_pool: ProjectionPool
) -> None:
    await projection_pool.execute("TRUNCATE projects, sessions, turns, messages, flags")

    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    started = recorded(session_log, TurnStarted(entry="spa", chars=4, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-a", started_seq=started.seq))
    session_log.record(SessionStarted(session_id=SESSION_B, session_kind="chat"))  # a newer session opened before A settled
    events = session_log.events()

    store = InMemoryStepStore()
    request = ModelRequest(parts=[UserPromptPart(content="A's question")])
    response = ModelResponse(parts=[TextPart(content="A's answer")], usage=RequestUsage(input_tokens=7, output_tokens=3))
    await store.save_snapshot(ContinuableSnapshot(run_id="run-a", step_index=0, messages=[request, response]))

    await refresh_live(projection_pool, events, store, project_root=tmp_path, only_session_id=SESSION_A)

    row = await projection_pool.fetchrow("SELECT turn_count, input_tokens FROM sessions WHERE id = $1", SESSION_A)
    assert row is not None, "the settle belongs to A, whatever the timeline opened after it"
    assert row["turn_count"] == 1
    assert row["input_tokens"] == 7
    assert await projection_pool.fetchrow("SELECT 1 FROM sessions WHERE id = $1", SESSION_B) is None


async def test_refresh_live_after_a_forget_replaces_the_sessions_turns_and_messages(
    tmp_path: Path, projection_pool: ProjectionPool
) -> None:
    """Two turns, then a Forget (mid-conversation, mimicking `forget_session`), then one more turn.

    Each step calls `refresh_live` with the session log as it stands at that moment, exactly as
    `refresh_projection` does after every settle and after a Forget clears the runtime. The two
    pre-Forget turns must not leave stale rows behind once the session next refreshes.
    """
    await projection_pool.execute("TRUNCATE projects, sessions, turns, messages, flags")

    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    store = InMemoryStepStore()

    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    started_1 = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-before-1", started_seq=started_1.seq))
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id="run-before-1",
            step_index=0,
            messages=[
                ModelRequest(parts=[UserPromptPart(content="first question")]),
                ModelResponse(parts=[TextPart(content="first answer")], usage=RequestUsage(input_tokens=10, output_tokens=5)),
            ],
        )
    )
    await refresh_live(projection_pool, session_log.events(), store, project_root=tmp_path, only_session_id=SESSION_A)

    started_2 = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-before-2", started_seq=started_2.seq))
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id="run-before-2",
            step_index=0,
            messages=[
                ModelRequest(parts=[UserPromptPart(content="second question")]),
                ModelResponse(parts=[TextPart(content="second answer")], usage=RequestUsage(input_tokens=20, output_tokens=8)),
            ],
        )
    )
    await refresh_live(projection_pool, session_log.events(), store, project_root=tmp_path, only_session_id=SESSION_A)

    pre_forget = await projection_pool.fetchrow("SELECT turn_count FROM sessions WHERE id = $1", SESSION_A)
    assert pre_forget is not None
    assert pre_forget["turn_count"] == 2, "both turns count before the Forget"

    session_log.record(ConversationForgotten(session_id=SESSION_A))
    await refresh_live(projection_pool, session_log.events(), store, project_root=tmp_path, only_session_id=SESSION_A)

    started_3 = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-after", started_seq=started_3.seq))
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id="run-after",
            step_index=0,
            messages=[
                ModelRequest(parts=[UserPromptPart(content="third question")]),
                ModelResponse(parts=[TextPart(content="third answer")], usage=RequestUsage(input_tokens=3, output_tokens=2)),
            ],
        )
    )
    await refresh_live(projection_pool, session_log.events(), store, project_root=tmp_path, only_session_id=SESSION_A)

    row = await projection_pool.fetchrow(
        "SELECT turn_count, input_tokens, output_tokens FROM sessions WHERE id = $1", SESSION_A
    )
    assert row is not None
    assert row["turn_count"] == 1, "only the turn after the Forget is counted"
    assert (row["input_tokens"], row["output_tokens"]) == (3, 2), "usage sums only the turn after the Forget"

    turn_rows = await projection_pool.fetch("SELECT run_id FROM turns WHERE session_id = $1", SESSION_A)
    assert [r["run_id"] for r in turn_rows] == ["run-after"], "the two pre-Forget turns must not linger as stale rows"

    message_rows = await projection_pool.fetch("SELECT DISTINCT run_id FROM messages")
    assert {r["run_id"] for r in message_rows} == {"run-after"}, "the pre-Forget messages must not linger as stale rows"

    events = session_log.events()
    await rebuild(projection_pool, events, store, project_root=tmp_path)
    rebuilt_row = await projection_pool.fetchrow(
        "SELECT turn_count, input_tokens, output_tokens FROM sessions WHERE id = $1", SESSION_A
    )
    assert rebuilt_row is not None
    assert (rebuilt_row["turn_count"], rebuilt_row["input_tokens"], rebuilt_row["output_tokens"]) == (
        row["turn_count"],
        row["input_tokens"],
        row["output_tokens"],
    ), "a rebuild from the same session log must agree with the incremental refresh"
    rebuilt_turns = await projection_pool.fetch("SELECT run_id FROM turns WHERE session_id = $1", SESSION_A)
    assert [r["run_id"] for r in rebuilt_turns] == ["run-after"]


async def test_interleaved_turns_attribute_to_the_session_that_started_them(
    tmp_path: Path, projection_pool: ProjectionPool
) -> None:
    """Two sessions answering at once: the stamps decide whose turn a line is, never its position.

    Both brackets are open at the same time and both settles land after both starts, which is what
    a positional fold gets wrong: it credits every turn, its usage, its messages and its excerpt
    to whichever session the file mentioned last.
    """
    pool = projection_pool
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(SessionStarted(session_id=SESSION_B, session_kind="chat"))
    started_a = recorded(session_log, TurnStarted(entry="spa", chars=12, session_id=SESSION_A, prompt_excerpt=None))
    started_b = recorded(session_log, TurnStarted(entry="acp", chars=13, session_id=SESSION_B, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-a", started_seq=started_a.seq))
    session_log.record(TurnSettled(run_id="run-b", started_seq=started_b.seq))
    events = session_log.events()

    store = InMemoryStepStore()
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id="run-a",
            step_index=0,
            messages=[
                ModelRequest(parts=[UserPromptPart(content="A asks first")]),
                ModelResponse(parts=[TextPart(content="A is answered")], usage=RequestUsage(input_tokens=7, output_tokens=3)),
            ],
        )
    )
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id="run-b",
            step_index=0,
            messages=[
                ModelRequest(parts=[UserPromptPart(content="B asks second")]),
                ModelResponse(parts=[TextPart(content="B is answered")], usage=RequestUsage(input_tokens=40, output_tokens=20)),
            ],
        )
    )

    await rebuild(pool, events, store, project_root=tmp_path)

    sessions = {
        str(row["id"]): row for row in await pool.fetch("SELECT id, title, turn_count, input_tokens, output_tokens FROM sessions")
    }
    assert set(sessions) == {SESSION_A, SESSION_B}
    assert (sessions[SESSION_A]["turn_count"], sessions[SESSION_B]["turn_count"]) == (1, 1)
    assert (sessions[SESSION_A]["input_tokens"], sessions[SESSION_A]["output_tokens"]) == (7, 3)
    assert (sessions[SESSION_B]["input_tokens"], sessions[SESSION_B]["output_tokens"]) == (40, 20)
    assert sessions[SESSION_A]["title"] == "A asks first"
    assert sessions[SESSION_B]["title"] == "B asks second", "a title is the excerpt of THIS session's own first run"

    turns = {row["seq"]: row for row in await pool.fetch("SELECT seq, session_id, run_id, entry, outcome FROM turns")}
    assert (str(turns[started_a.seq]["session_id"]), turns[started_a.seq]["run_id"]) == (SESSION_A, "run-a")
    assert (str(turns[started_b.seq]["session_id"]), turns[started_b.seq]["run_id"]) == (SESSION_B, "run-b")
    assert {turns[started_a.seq]["entry"], turns[started_b.seq]["entry"]} == {"spa", "acp"}
    assert {row["outcome"] for row in turns.values()} == {"settled"}

    said: dict[str, list[str]] = {}
    rows = await pool.fetch(
        "SELECT t.session_id, m.text_content FROM turns t JOIN messages m ON m.run_id = t.run_id ORDER BY t.session_id, m.idx"
    )
    for row in rows:
        said.setdefault(str(row["session_id"]), []).append(str(row["text_content"]))
    assert said[SESSION_A] == ["A asks first", "A is answered"]
    assert said[SESSION_B] == ["B asks second", "B is answered"]


class _BrokenStore:
    """A `StepStore` whose `latest_snapshot` always raises -- exercises `refresh_live`'s guard."""

    async def register_run(self, record: RunRecord) -> None:
        pass

    async def get_run(self, *, run_id: str) -> RunRecord | None:
        return None

    async def list_runs(self, *, parent_run_id: str | None = None, conversation_id: str | None = None) -> list[RunRecord]:
        return []

    async def append_event(self, event: StepEvent) -> None:
        pass

    async def list_events(self, *, run_id: str) -> list[StepEvent]:
        return []

    async def save_snapshot(self, snapshot: ContinuableSnapshot) -> None:
        pass

    async def latest_snapshot(self, *, run_id: str, include_interrupted: bool = False) -> ContinuableSnapshot | None:
        msg = "boom"
        raise RuntimeError(msg)

    async def record_tool_effect(self, record: ToolEffectRecord) -> None:
        pass

    async def get_tool_effect(self, *, run_id: str, tool_call_id: str) -> ToolEffectRecord | None:
        return None

    async def list_unresolved_tool_effects(self, *, run_id: str) -> list[ToolEffectRecord]:
        return []


async def test_refresh_live_never_raises_on_a_broken_store(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=4, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-broken", started_seq=2))
    events = session_log.events()

    await refresh_live(projection_pool, events, _BrokenStore(), project_root=tmp_path)  # must not raise


async def test_delete_session_rows_removes_rows_and_returns_run_ids(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    fixture = await _rebuilt(projection_pool, tmp_path)
    del fixture

    run_ids = await delete_session_rows(projection_pool, SESSION_A)

    assert set(run_ids) == {"run-1", "run-2"}
    assert await projection_pool.fetchrow("SELECT 1 FROM sessions WHERE id = $1", SESSION_A) is None
    assert await projection_pool.fetchrow("SELECT 1 FROM turns WHERE session_id = $1", SESSION_A) is None
    assert await projection_pool.fetchrow("SELECT 1 FROM messages WHERE run_id = 'run-1'") is None
    assert await projection_pool.fetchrow("SELECT 1 FROM messages WHERE run_id = 'run-2'") is None


async def test_friction_flagged_folds_into_a_flag_row(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=4, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-flagged", started_seq=2))
    flagged = recorded(
        session_log,
        FrictionFlagged(
            target_kind="tool",
            target="shell",
            note="took three retries",
            tool_call_id="call-1",
            source="live",
            subject_session_id=None,
        ),
    )
    events = session_log.events()
    store = InMemoryStepStore()

    await rebuild(projection_pool, events, store, project_root=tmp_path)

    row = await projection_pool.fetchrow("SELECT session_id, log_seq, target_kind, target, note FROM flags")
    assert row is not None
    assert str(row["session_id"]) == SESSION_A
    assert row["log_seq"] == flagged.seq
    assert row["target_kind"] == "tool"
    assert row["target"] == "shell"
    assert row["note"] == "took three retries"


async def test_live_flag_with_a_stamped_subject_lands_on_it_not_a_later_session(
    tmp_path: Path, projection_pool: ProjectionPool
) -> None:
    """A live flag now self-identifies its subject; a newer session opened after it must not steal it."""
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=4, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(SessionStarted(session_id=SESSION_B, session_kind="chat"))  # a newer session opens while A's turn is still open
    session_log.record(
        FrictionFlagged(
            target_kind="tool",
            target="shell",
            note="took three retries",
            tool_call_id=None,
            source="live",
            subject_session_id=SESSION_A,
        )
    )
    events = session_log.events()
    store = InMemoryStepStore()

    await rebuild(projection_pool, events, store, project_root=tmp_path)

    row = await projection_pool.fetchrow("SELECT session_id FROM flags")
    assert row is not None
    assert str(row["session_id"]) == SESSION_A


def _isolated_sessions_database(monkeypatch: pytest.MonkeyPatch, *, dsn: str) -> Callable[[], Awaitable[asyncpg.Pool]]:
    """Point every name a CLI invocation reaches the sessions database through at `dsn`.

    The verb takes its pools from the container, so the wiring module's own bound names are as
    live as the store module's: both the pool it opens and the DSN the record's pool is opened
    from. A CLI invocation a test drives must never reach the developer's own
    `aifactory_sessions`, which the verb truncates. `dsn` says `sslmode` for the same reason the
    integration fixture does: the record's opener reads its ssl terms off the DSN.

    Returns:
        How to open a pool onto `dsn`, for the reference rebuild a test runs itself.
    """
    import harness.adapters.store
    import harness.adapters.wiring

    async def open_it() -> asyncpg.Pool:
        return await asyncpg.create_pool(dsn)

    def name_it() -> str:
        return dsn

    for module in (harness.adapters.store, harness.adapters.wiring):
        monkeypatch.setattr(module, "sessions_pool", open_it)
        monkeypatch.setattr(module, "sessions_writer_dsn", name_it)
    return open_it


def test_sync_rebuild_matches_direct_rebuild_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg: str) -> None:
    """`harness sessions rebuild` sweeps the checkout plus every registered project.

    The reference is `rebuild()` run directly against each home in turn, summed: the CLI must
    land on the same total row counts, not merely "some rows". The verb takes its pools from the
    container, so every name a CLI invocation can reach the sessions database through is redirected
    at the isolated container -- one this test drives must never reach the developer's live
    `aifactory_sessions`, which it would truncate.
    """
    import asyncio

    import harness.settings

    from harness.cli import app as cli_app

    _isolated_pool = _isolated_sessions_database(monkeypatch, dsn=as_asyncpg_dsn(pg))

    checkout_root = tmp_path / "checkout"
    checkout_root.mkdir()
    monkeypatch.setattr(harness.settings, "CHECKOUT", checkout_root)

    project_root = tmp_path / "client"
    project_root.mkdir()

    checkout_journal = SessionLog(checkout_root / ".state" / "session-log.jsonl", clock=_clock, topics=None)
    checkout_journal.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    checkout_journal.record(TurnStarted(entry="spa", chars=4, session_id=SESSION_A, prompt_excerpt=None))
    checkout_journal.record(TurnSettled(run_id="run-checkout", started_seq=2))

    workspace = open_workspace(project_root)
    workspace.session_log.record(SessionStarted(session_id=SESSION_B, session_kind="chat"))
    workspace.session_log.record(TurnStarted(entry="spa", chars=6, session_id=SESSION_B, prompt_excerpt=None))
    workspace.session_log.record(TurnSettled(run_id="run-project", started_seq=2))

    project_registry.install(ProjectsLog(checkout_root / ".state" / "projects-log.jsonl"))
    project_registry.register(project_root, project_id=service.project_id_of(project_root))

    tables = ("projects", "sessions", "turns")

    async def _rebuilt_counts(events: tuple[Recorded, ...], store: StepStore, root: Path) -> dict[str, int]:
        pool = await _isolated_pool()
        projected = PostgresProjectionPool(pool)
        try:
            await ensure_schema(projected)
            await rebuild(projected, events, store, project_root=root)
            return {table: await pool.fetchval(f"SELECT count(*) FROM {table}") for table in tables}  # noqa: S608 -- fixed local literal set
        finally:
            await pool.close()

    # One record serves the checkout and every project, so the reference reads both homes through one store.
    record = InMemoryStepStore()
    expected_checkout = asyncio.run(_rebuilt_counts(checkout_journal.events(), record, checkout_root))
    expected_project = asyncio.run(_rebuilt_counts(workspace.session_log.events(), record, project_root))
    expected = {table: expected_checkout[table] + expected_project[table] for table in tables}

    # The verb reads its own state home off a settings root of its own, so the run is pointed at
    # this checkout's `.state/` the way a deployment points it anywhere: by naming the one field.
    result = runner.invoke(cli_app, ["sessions", "rebuild"], env={"APP_STATE__HOME": str(checkout_root / ".state")})
    assert result.exit_code == 0

    async def _actual_counts() -> dict[str, int]:
        pool = await _isolated_pool()
        try:
            return {table: await pool.fetchval(f"SELECT count(*) FROM {table}") for table in tables}  # noqa: S608 -- same fixed local literal set
        finally:
            await pool.close()

    try:
        assert asyncio.run(_actual_counts()) == expected
    finally:
        project_registry._REGISTRY = None
