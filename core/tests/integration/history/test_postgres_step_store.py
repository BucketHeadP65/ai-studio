"""The store against a real Postgres."""

import asyncio
import json

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from asyncpg.exceptions import UniqueViolationError
from pydantic_ai_harness.step_persistence import RunRecord, StepEvent
from pydantic_core import to_jsonable_python

from core.ai.history.contracts import PoolDatabase, PostgresStepStore, create_record_schema
from core.ai.history.conversation_record import ConversationRecord
from tests.integration.conftest import DATABASE_URL, TEST_SCHEMA
from tests.unit.history.conftest import CLEARED, RESULT, snapshot, turn_messages

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
LATER = AT + timedelta(minutes=1)
INSERT_MESSAGE = """
    INSERT INTO messages (conversation_id, ordinal, key, kind, first_run_id, first_seq, created_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
"""
SELECT_FORM_DIGESTS = "SELECT content_digest FROM message_forms WHERE conversation_id = $1 ORDER BY ordinal, form"
SELECT_STAMP_DIGESTS = "SELECT stamps_digest FROM message_stamps WHERE conversation_id = $1 ORDER BY ordinal, version"
SELECT_EVENT_SEQS = "SELECT seq FROM run_events WHERE run_id = $1 ORDER BY seq"


@pytest.fixture
def store(database):
    return PostgresStepStore(database)


@pytest.fixture
def conversation_id() -> str:
    """The record needs only an id, the conversations table is the application's."""
    return str(uuid4())


async def event_seqs(database, run_id: str) -> list[int]:
    """The numbers the run's events were written under, in order."""
    async with database.acquire() as connection:
        rows = await connection.fetch(SELECT_EVENT_SEQS, run_id)
    return [row["seq"] for row in rows]


class TestRegistration:
    async def test_a_run_comes_back_with_the_registration_that_claimed_it(self, store, conversation_id):
        run_id = "run-claimed-" + conversation_id
        await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id, started_at=AT, registration_id="registration-1"))

        found = await store.get_run(run_id=run_id)
        listed = await store.list_runs(conversation_id=conversation_id)

        assert found is not None
        assert found.registration_id == "registration-1"
        assert [run.registration_id for run in listed] == ["registration-1"]

    async def test_reapplying_the_schema_waits_on_no_reader_of_its_tables(self, database):
        impatient = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=1, server_settings={"search_path": f"{TEST_SCHEMA},public", "lock_timeout": "500"}
        )
        try:
            async with database.acquire() as reader, reader.transaction():
                await reader.execute("LOCK TABLE runs, run_events IN ACCESS SHARE MODE")
                await create_record_schema(PoolDatabase(impatient))
        finally:
            await impatient.close()


class TestSaveAndLoad:
    async def test_two_runs_reconstruct_exactly_and_share_their_messages(self, store, conversation_id):
        first = turn_messages()
        run_a, run_b = "run-a-" + conversation_id, "run-b-" + conversation_id
        await store.register_run(RunRecord(run_id=run_a, conversation_id=conversation_id, started_at=AT))
        await store.save_snapshot(snapshot(run_a, 1, first[:2], conversation_id=conversation_id))
        await store.save_snapshot(snapshot(run_a, 2, first, conversation_id=conversation_id))
        await store.register_run(RunRecord(run_id=run_b, conversation_id=conversation_id, started_at=AT))
        await store.save_snapshot(snapshot(run_b, 1, turn_messages(cleared=True), conversation_id=conversation_id))

        latest_a = await store.latest_snapshot(run_id=run_a)
        latest_b = await store.latest_snapshot(run_id=run_b)
        history = await store.history_for_next_run(conversation_id=conversation_id)
        shown = await store.conversation_messages(conversation_id=conversation_id)

        assert latest_a is not None
        assert latest_b is not None
        assert to_jsonable_python(latest_a.messages) == to_jsonable_python(first)
        assert latest_b.messages[2].parts[0].content == CLEARED
        assert to_jsonable_python(history) == to_jsonable_python(latest_b.messages)
        assert shown[2].parts[0].content == RESULT

    async def test_every_step_of_a_run_comes_back_holding_what_it_held(self, store, conversation_id):
        run_id = "run-s-" + conversation_id
        await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id, started_at=AT))
        await store.save_snapshot(snapshot(run_id, 1, turn_messages(), conversation_id=conversation_id))
        await store.save_snapshot(snapshot(run_id, 2, turn_messages(cleared=True), conversation_id=conversation_id))

        held = await store.list_snapshots(run_id=run_id)

        assert [step.step_index for step in held] == [1, 2]
        assert held[0].messages[2].parts[0].content == RESULT
        assert held[1].messages[2].parts[0].content == CLEARED

    async def test_interrupted_snapshots_stay_off_the_default_read(self, store, conversation_id):
        run_id = "run-c-" + conversation_id
        await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id, started_at=AT))
        await store.save_snapshot(snapshot(run_id, 1, turn_messages()[:2], conversation_id=conversation_id))
        await store.save_snapshot(snapshot(run_id, 2, turn_messages()[:3], state="interrupted", conversation_id=conversation_id))

        complete = await store.latest_snapshot(run_id=run_id)
        interrupted = await store.latest_snapshot(run_id=run_id, include_interrupted=True)

        assert complete is not None
        assert interrupted is not None
        assert (complete.step_index, interrupted.step_index) == (1, 2)


class TestDigests:
    async def test_the_digest_columns_are_what_the_record_diffs_by(self, store, database, conversation_id):
        run_id = "run-n-" + conversation_id
        messages = turn_messages()
        await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id, started_at=AT))
        await store.save_snapshot(snapshot(run_id, 1, messages, conversation_id=conversation_id))
        await store.save_snapshot(snapshot(run_id, 2, turn_messages(cleared=True), conversation_id=conversation_id))
        record = ConversationRecord()
        record.record(snapshot(run_id, 1, messages, conversation_id=conversation_id), seq=0)
        record.record(snapshot(run_id, 2, turn_messages(cleared=True), conversation_id=conversation_id), seq=1)

        async with database.acquire() as connection:
            forms = await connection.fetch(SELECT_FORM_DIGESTS, UUID(conversation_id))
            stamps = await connection.fetch(SELECT_STAMP_DIGESTS, UUID(conversation_id))

        assert [row["content_digest"] for row in forms] == [digest for digests in record.forms.values() for digest in digests]
        assert [row["stamps_digest"] for row in stamps] == [digest for digests in record.stamps.values() for digest in digests]


class TestMessageKeys:
    async def test_one_key_takes_one_ordinal_in_a_conversation(self, database, conversation_id):
        row = (UUID(conversation_id), "d" * 32, "request", "run-d-" + conversation_id, 0, AT)
        async with database.acquire() as connection, connection.transaction():
            await connection.execute(INSERT_MESSAGE, row[0], 0, *row[1:])

        with pytest.raises(UniqueViolationError):
            async with database.acquire() as connection, connection.transaction():
                await connection.execute(INSERT_MESSAGE, row[0], 1, *row[1:])


class TestEvents:
    async def test_each_event_is_numbered_one_past_the_last_the_run_holds(self, store, database, conversation_id):
        run_id = "run-p-" + conversation_id
        await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id, started_at=AT))

        await store.append_event(StepEvent(run_id=run_id, kind="run_started", step_index=0, timestamp=AT))
        await store.append_event(StepEvent(run_id=run_id, kind="run_completed", step_index=1, timestamp=LATER))

        assert await event_seqs(database, run_id) == [0, 1]

    async def test_events_recorded_at_once_are_numbered_without_a_collision(self, store, database, conversation_id):
        run_id = "run-e-" + conversation_id
        await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id, started_at=AT))

        await asyncio.gather(
            *(store.append_event(StepEvent(run_id=run_id, kind="tool_call_started", step_index=1, timestamp=AT)) for _ in range(5))
        )

        assert await event_seqs(database, run_id) == [0, 1, 2, 3, 4]


class TestRequestParameters:
    async def test_the_same_parameters_are_kept_once_and_the_step_points_at_them(self, store, database, conversation_id):
        run_id = "run-f-" + conversation_id
        await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id, started_at=AT))
        parameters = {"model": "test", "request_parameters": {"function_tools": [{"name": "lookup_city"}]}}

        await store.record_request_parameters(run_id=run_id, step_index=1, parameters=parameters)
        await store.record_request_parameters(run_id=run_id, step_index=1, parameters=parameters)

        async with database.acquire() as connection:
            pointers = await connection.fetch("SELECT digest FROM step_parameters WHERE run_id = $1", run_id)
            kept = await connection.fetch("SELECT parameters FROM request_parameters WHERE digest = $1", pointers[0]["digest"])

        assert len(pointers) == 1
        assert len(kept) == 1
        assert json.loads(kept[0]["parameters"]) == parameters


class TestParentRuns:
    async def test_a_run_a_tool_started_is_not_what_the_next_run_continues_from(self, store, conversation_id):
        parent, child = "run-g-" + conversation_id, "run-h-" + conversation_id
        await store.register_run(RunRecord(run_id=parent, conversation_id=conversation_id, started_at=AT))
        await store.save_snapshot(snapshot(parent, 1, turn_messages(), conversation_id=conversation_id))
        await store.register_run(RunRecord(run_id=child, conversation_id=conversation_id, parent_run_id=parent, started_at=LATER))
        await store.save_snapshot(snapshot(child, 1, turn_messages(cleared=True), conversation_id=conversation_id))

        history = await store.history_for_next_run(conversation_id=conversation_id)
        continued = await store.latest_snapshot(run_id=parent)

        assert continued is not None
        assert history[2].parts[0].content == RESULT
        assert to_jsonable_python(history) == to_jsonable_python(continued.messages)


class TestListRuns:
    async def test_filters_compose_over_the_ordered_list(self, store, conversation_id):
        other = str(uuid4())
        parent, child, elsewhere = "run-k-" + conversation_id, "run-l-" + conversation_id, "run-m-" + other
        await store.register_run(RunRecord(run_id=parent, conversation_id=conversation_id, started_at=AT + timedelta(minutes=2)))
        await store.register_run(RunRecord(run_id=child, conversation_id=conversation_id, parent_run_id=parent, started_at=LATER))
        await store.register_run(RunRecord(run_id=elsewhere, conversation_id=other, started_at=AT))

        assert [run.run_id for run in await store.list_runs()] == [elsewhere, child, parent]
        assert [run.run_id for run in await store.list_runs(conversation_id=conversation_id)] == [child, parent]
        assert [run.run_id for run in await store.list_runs(parent_run_id=parent)] == [child]
        assert [run.run_id for run in await store.list_runs(conversation_id=conversation_id, parent_run_id=parent)] == [child]


class TestDeleteConversation:
    async def test_one_conversation_goes_and_another_stays(self, store, database, conversation_id):
        other = str(uuid4())
        going, staying = "run-r-" + conversation_id, "run-s-" + other
        for run_id, conversation in ((going, conversation_id), (staying, other)):
            await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation, started_at=AT))
            await store.save_snapshot(snapshot(run_id, 1, turn_messages(), conversation_id=conversation))
            await store.append_event(StepEvent(run_id=run_id, kind="run_started", step_index=0, timestamp=AT))

        await store.delete_conversation(conversation_id)

        async with database.acquire() as connection:
            runs = await connection.fetch("SELECT run_id FROM runs")
            messages = await connection.fetch("SELECT conversation_id FROM messages")
            forms = await connection.fetch("SELECT conversation_id FROM message_forms")
            steps = await connection.fetch("SELECT run_id FROM steps")
            events = await connection.fetch("SELECT run_id FROM run_events")

        assert [row["run_id"] for row in runs] == [staying]
        assert {row["conversation_id"] for row in messages} == {UUID(other)}
        assert {row["conversation_id"] for row in forms} == {UUID(other)}
        assert {row["run_id"] for row in steps} == {staying}
        assert {row["run_id"] for row in events} == {staying}
        assert await store.history_for_next_run(conversation_id=conversation_id) == []
