"""The record's events as a cursor consumer reads them: after a position, of some kinds, with the run they belong to."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from pydantic_ai_harness.step_persistence import RunRecord, StepEvent

from core.ai.history.contracts import PoolDatabase, PostgresStepStore, create_record_schema, reads
from tests.integration.conftest import TEST_SCHEMA

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WANTED_KINDS = ("model_request_completed", "run_completed")
SELECT_ID_COLUMNS = (
    "SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = 'run_events' AND column_name = 'id'"
)
SELECT_ID_INDEXES = (
    "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = 'run_events' AND indexname = 'run_events_id_key'"
)


@pytest.fixture
def store(database):
    return PostgresStepStore(database)


@pytest.fixture
def conversation_id() -> str:
    return str(uuid4())


async def _write_five_events(store: PostgresStepStore) -> tuple[str, str, str, str]:
    """Two runs across two conversations, five events mixed in kind, in insertion order."""
    conversation_a, conversation_b = str(uuid4()), str(uuid4())
    run_a, run_b = "run-a-" + conversation_a, "run-b-" + conversation_b
    await store.register_run(RunRecord(run_id=run_a, conversation_id=conversation_a, agent_name="agent-a", started_at=AT))
    await store.register_run(RunRecord(run_id=run_b, conversation_id=conversation_b, agent_name="agent-b", started_at=AT))
    await store.append_event(StepEvent(run_id=run_a, kind="run_started", step_index=0, timestamp=AT))
    await store.append_event(StepEvent(run_id=run_a, kind="model_request_completed", step_index=0, timestamp=AT + timedelta(minutes=1)))
    await store.append_event(
        StepEvent(
            run_id=run_a,
            kind="tool_call_started",
            step_index=1,
            timestamp=AT + timedelta(minutes=2),
            tool_call_id="call_1",
            tool_name="lookup_city",
        )
    )
    await store.append_event(StepEvent(run_id=run_b, kind="model_request_completed", step_index=0, timestamp=AT + timedelta(minutes=3)))
    await store.append_event(StepEvent(run_id=run_b, kind="run_completed", step_index=1, timestamp=AT + timedelta(minutes=4)))
    return run_a, run_b, conversation_a, conversation_b


class TestEventsAfter:
    async def test_matching_kinds_come_back_ordered_by_id_with_the_run_they_belong_to(self, store, database):
        run_a, run_b, conversation_a, conversation_b = await _write_five_events(store)

        async with database.acquire() as connection:
            matches = await reads.events_after(connection, 0, kinds=WANTED_KINDS, limit=10)

        assert [match.kind for match in matches] == ["model_request_completed", "model_request_completed", "run_completed"]
        assert [match.id for match in matches] == sorted(match.id for match in matches)
        assert (matches[0].run_id, matches[0].agent_name, matches[0].conversation_id, matches[0].step_index) == (
            run_a,
            "agent-a",
            UUID(conversation_a),
            0,
        )
        assert matches[0].at == AT + timedelta(minutes=1)
        assert (matches[1].run_id, matches[1].agent_name, matches[1].conversation_id, matches[1].step_index) == (
            run_b,
            "agent-b",
            UUID(conversation_b),
            0,
        )
        assert (matches[2].run_id, matches[2].kind, matches[2].step_index) == (run_b, "run_completed", 1)
        assert all(match.kind != "tool_call_started" for match in matches)

    async def test_the_cursor_advances_past_a_handled_position_and_the_limit_caps_the_page(self, store, database):
        await _write_five_events(store)

        async with database.acquire() as connection:
            first_batch = await reads.events_after(connection, 0, kinds=WANTED_KINDS, limit=10)
            none_left = await reads.events_after(connection, first_batch[-1].id, kinds=WANTED_KINDS, limit=10)
            capped = await reads.events_after(connection, 0, kinds=WANTED_KINDS, limit=1)

        assert none_left == []
        assert capped == [first_batch[0]]


class TestLastPosition:
    async def test_an_empty_record_answers_zero(self, database: PoolDatabase) -> None:
        async with database.acquire() as connection:
            assert await reads.last_event_position(connection) == 0

    async def test_the_last_position_is_the_highest_id(self, store: PostgresStepStore, database: PoolDatabase) -> None:
        await _write_five_events(store)

        async with database.acquire() as connection:
            highest = (await reads.events_after(connection, 0, kinds=WANTED_KINDS, limit=100))[-1].id
            assert await reads.last_event_position(connection) == highest


class TestRunsEndedSince:
    async def test_pages_resume_after_the_last_position(self, store: PostgresStepStore, database: PoolDatabase) -> None:
        run_a, run_b, _, _ = await _write_five_events(store)
        await store.append_event(StepEvent(run_id=run_a, kind="run_failed", step_index=2, timestamp=AT + timedelta(minutes=6)))

        async with database.acquire() as connection:
            first = await reads.runs_ended_since(connection, AT, limit=1)
            second = await reads.runs_ended_since(connection, AT, limit=1, after_position=first[-1].id)
            final = await reads.runs_ended_since(connection, AT, limit=1, after_position=second[-1].id)

        assert [item.run_id for item in first + second] == [run_b, run_a]
        assert final == []

    async def test_only_terminal_events_in_the_window_answer(self, store: PostgresStepStore, database: PoolDatabase) -> None:
        _, run_b, _, _ = await _write_five_events(store)

        async with database.acquire() as connection:
            inside = await reads.runs_ended_since(connection, AT + timedelta(minutes=4), limit=100)
            outside = await reads.runs_ended_since(connection, AT + timedelta(minutes=5), limit=100)

        assert [(match.run_id, match.kind) for match in inside] == [(run_b, "run_completed")]
        assert outside == []

    async def test_the_window_start_is_inclusive_and_the_order_is_by_position(
        self, store: PostgresStepStore, database: PoolDatabase
    ) -> None:
        run_a, run_b, _, _ = await _write_five_events(store)
        await store.append_event(StepEvent(run_id=run_a, kind="run_completed", step_index=2, timestamp=AT + timedelta(minutes=6)))

        async with database.acquire() as connection:
            both = await reads.runs_ended_since(connection, AT, limit=100)
            later_only = await reads.runs_ended_since(connection, AT + timedelta(minutes=6), limit=100)

        assert [match.run_id for match in both] == [run_b, run_a]
        assert [match.id for match in both] == sorted(match.id for match in both)
        assert [match.run_id for match in later_only] == [run_a]


class TestSchemaIdempotence:
    async def test_creating_the_schema_twice_leaves_one_id_column_and_one_unique_index(self, database):
        await create_record_schema(database)

        async with database.acquire() as connection:
            columns = await connection.fetch(SELECT_ID_COLUMNS, TEST_SCHEMA)
            indexes = await connection.fetch(SELECT_ID_INDEXES, TEST_SCHEMA)

        assert len(columns) == 1
        assert len(indexes) == 1
