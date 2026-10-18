import re

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from polyfactory.factories.dataclass_factory import DataclassFactory
from pydantic import ValidationError
from pydantic_ai_harness.step_persistence import RunRecord, StepEvent, ToolEffectRecord

from core.ai.history.contracts import PostgresStepStore
from core.ai.history.infrastructure.queries import (
    DELETE_CONVERSATION_CONTINUATION_ATTEMPTS,
    DELETE_CONVERSATION_CONTINUATION_HEADS,
    DELETE_CONVERSATION_MESSAGES,
    DELETE_CONVERSATION_RUNS,
    INSERT_EVENT,
    INSERT_RUN,
    INSERT_STEP,
    KEEP_REQUEST_PARAMETERS,
    LOCK_RUN,
    NEXT_STEP_SEQ,
    POINT_STEP_AT_PARAMETERS,
    SELECT_COMPLETE_STEPS,
    SELECT_CONVERSATION_DIGESTS,
    SELECT_RUN,
    SELECT_STARTED_TOOL_EFFECTS,
    SELECT_STEPS,
    UPSERT_TOOL_EFFECT,
)
from tests.support.fakes.database import FakeDatabase
from tests.unit.history.conftest import snapshot, turn_messages

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
CONVERSATION = "0f8c1d2e-1111-4222-8333-444455556666"
NO_ROWS: list[dict[str, object]] = []
RUN_ROW: dict[str, object] = {
    "run_id": "run-a",
    "conversation_id": CONVERSATION,
    "parent_run_id": None,
    "agent_name": None,
    "started_at": AT,
    "metadata": {},
    "registration_id": None,
}
EMPTY_CONVERSATION = [[RUN_ROW], NO_ROWS, [{"seq": 0}]]
"""The run, then a conversation holding no message at all, then 0 as the step's next seq."""
EVENT_ROW: dict[str, object] = {
    "run_id": "run-a",
    "seq": 0,
    "kind": "run_started",
    "step_index": 0,
    "at": AT,
    "tool_call_id": None,
    "tool_name": None,
    "error": None,
    "metadata": {},
}
TOOL_EFFECT_ROW: dict[str, object] = {
    "run_id": "run-a",
    "tool_call_id": "call_1",
    "tool_name": "lookup_city",
    "status": "started",
    "started_at": AT,
    "ended_at": None,
    "idempotency_key": None,
    "effect_summary": None,
}


def ran(database: FakeDatabase, index: int) -> tuple[str, tuple[object, ...]]:
    """The statement at `index` and the arguments it was run with."""
    return database.opened.executed[index]


def written(database: FakeDatabase) -> list[str]:
    """The tables the statements executed so far wrote to, in order."""
    return [found.group(1) for statement in database.opened.statements() if (found := re.search(r"INSERT INTO (\w+)", statement))]


class RunRecordFactory(DataclassFactory[RunRecord]):
    """A run whose field values are not what the test asserts on."""

    conversation_id = CONVERSATION


class StepEventFactory(DataclassFactory[StepEvent]):
    """An event whose field values are not what the test asserts on."""


class ToolEffectRecordFactory(DataclassFactory[ToolEffectRecord]):
    """A tool effect whose field values are not what the test asserts on."""


class TestRegisterRun:
    async def test_refuses_a_run_without_a_conversation(self):
        database = FakeDatabase()
        store = PostgresStepStore(database)

        with pytest.raises(ValueError, match="conversation"):
            await store.register_run(RunRecord(run_id="run-a"))

    async def test_inserts_the_run(self):
        database = FakeDatabase()
        store = PostgresStepStore(database)

        await store.register_run(RunRecordFactory.build())

        assert written(database) == ["runs"]
        assert ran(database, 0)[0] is INSERT_RUN

    async def test_the_metadata_goes_out_as_json_text(self):
        database = FakeDatabase()
        store = PostgresStepStore(database)

        await store.register_run(RunRecord(run_id="run-a", conversation_id=CONVERSATION, started_at=AT, metadata={"agent": "chat"}))

        assert ran(database, 0)[1][5] == '{"agent":"chat"}'

    async def test_the_registration_goes_out_with_the_run(self):
        database = FakeDatabase()
        store = PostgresStepStore(database)

        await store.register_run(RunRecord(run_id="run-a", conversation_id=CONVERSATION, started_at=AT, registration_id="registration-1"))

        assert ran(database, 0)[1][6] == "registration-1"


class TestGetRun:
    async def test_returns_none_when_absent(self):
        database = FakeDatabase(results=[NO_ROWS])
        store = PostgresStepStore(database)

        assert await store.get_run(run_id="run-x") is None

    async def test_maps_the_row_to_a_run_record(self):
        row = {**RUN_ROW, "metadata": '{"agent": "chat"}'}
        database = FakeDatabase(results=[[row]])
        store = PostgresStepStore(database)

        record = await store.get_run(run_id="run-a")

        assert record == RunRecord(run_id="run-a", conversation_id=CONVERSATION, started_at=AT, metadata={"agent": "chat"})

    async def test_the_registration_that_claimed_the_run_comes_back_with_it(self):
        row = {**RUN_ROW, "metadata": "{}", "registration_id": "registration-1"}
        database = FakeDatabase(results=[[row]])
        store = PostgresStepStore(database)

        record = await store.get_run(run_id="run-a")

        assert record is not None
        assert record.registration_id == "registration-1"

    async def test_refuses_metadata_whose_values_are_not_strings(self):
        row = {**RUN_ROW, "metadata": '{"agent": 1}'}
        database = FakeDatabase(results=[[row]])
        store = PostgresStepStore(database)

        with pytest.raises(ValidationError):
            await store.get_run(run_id="run-a")


class TestEvents:
    async def test_locks_the_run_before_it_numbers_the_event(self):
        database = FakeDatabase()
        store = PostgresStepStore(database)

        await store.append_event(StepEventFactory.build())

        assert ran(database, 0)[0] is LOCK_RUN
        assert "pg_advisory_xact_lock" in ran(database, 0)[0]

    async def test_the_append_binds_the_event_and_leaves_the_number_to_the_insert(self):
        database = FakeDatabase()
        store = PostgresStepStore(database)

        await store.append_event(StepEvent(run_id="run-a", kind="run_started", step_index=0, timestamp=AT))

        statement, arguments = ran(database, -1)

        assert statement is INSERT_EVENT
        assert arguments[:4] == ("run-a", "run_started", 0, AT)
        assert len(arguments) == 8

    async def test_list_events_maps_rows(self):
        database = FakeDatabase(results=[[EVENT_ROW]])
        store = PostgresStepStore(database)

        events = await store.list_events(run_id="run-a")

        assert events == [StepEvent(run_id="run-a", kind="run_started", step_index=0, timestamp=AT)]


class TestSaveSnapshot:
    async def test_refuses_a_snapshot_naming_another_conversation(self):
        database = FakeDatabase(results=[[RUN_ROW]])
        store = PostgresStepStore(database)

        with pytest.raises(ValueError, match="names conversation"):
            await store.save_snapshot(snapshot("run-a", 1, turn_messages(), conversation_id=str(uuid4())))

    async def test_a_write_reads_the_digests_and_no_content(self):
        database = FakeDatabase(results=EMPTY_CONVERSATION)
        store = PostgresStepStore(database)

        await store.save_snapshot(snapshot("run-a", 1, turn_messages(), conversation_id=CONVERSATION))

        statements = database.opened.statements()
        read = statements[: statements.index(INSERT_STEP)]
        assert read == [SELECT_RUN, SELECT_CONVERSATION_DIGESTS, NEXT_STEP_SEQ]

    async def test_the_step_goes_in_before_the_messages_it_names(self):
        database = FakeDatabase(results=EMPTY_CONVERSATION)
        store = PostgresStepStore(database)

        await store.save_snapshot(snapshot("run-a", 1, turn_messages(), conversation_id=CONVERSATION))

        assert written(database) == ["steps", "instructions", "messages", "message_forms", "message_stamps"]

    async def test_the_step_carries_its_entries_as_flat_triples(self):
        database = FakeDatabase(results=EMPTY_CONVERSATION)
        store = PostgresStepStore(database)

        await store.save_snapshot(snapshot("run-a", 1, turn_messages(), conversation_id=CONVERSATION))

        arguments = next(arguments for statement, arguments in database.opened.executed if statement is INSERT_STEP)
        assert arguments[:5] == ("run-a", 0, 1, AT, "complete")
        assert arguments[5] == [0, 0, 0, 1, 0, 0, 2, 0, 0, 3, 0, 0]


class TestToolEffects:
    async def test_record_upserts_on_run_and_call_id(self):
        database = FakeDatabase()
        store = PostgresStepStore(database)

        await store.record_tool_effect(ToolEffectRecordFactory.build())

        statement = ran(database, 0)[0]
        assert statement is UPSERT_TOOL_EFFECT
        assert written(database) == ["tool_effects"]
        assert "ON CONFLICT" in statement
        assert "DO UPDATE" in statement

    async def test_unresolved_are_the_started_ones(self):
        database = FakeDatabase(results=[[TOOL_EFFECT_ROW]])
        store = PostgresStepStore(database)

        effects = await store.list_unresolved_tool_effects(run_id="run-a")

        assert [effect.tool_call_id for effect in effects] == ["call_1"]
        assert ran(database, 0)[0] is SELECT_STARTED_TOOL_EFFECTS
        assert "status = 'started'" in ran(database, 0)[0]


class TestRecordRequestParameters:
    async def test_writes_the_digest_row_then_the_step_row(self):
        database = FakeDatabase()
        store = PostgresStepStore(database)

        await store.record_request_parameters(run_id="run-a", step_index=1, parameters={"model": "test"})

        kept, pointed = database.opened.statements()
        assert written(database) == ["request_parameters", "step_parameters"]
        assert kept is KEEP_REQUEST_PARAMETERS
        assert pointed is POINT_STEP_AT_PARAMETERS
        assert ran(database, 0)[1][1] == '{"model":"test"}'
        assert "ON CONFLICT" in kept
        assert "DO NOTHING" in kept
        assert "ON CONFLICT" in pointed
        assert "DO UPDATE" in pointed


class TestListSnapshots:
    async def test_every_step_of_the_run_comes_back_oldest_first(self):
        steps: list[dict[str, object]] = [
            {"seq": 0, "step_index": 1, "at": AT, "state": "complete", "entries": []},
            {"seq": 1, "step_index": 2, "at": AT, "state": "complete", "entries": []},
        ]
        database = FakeDatabase(results=[[RUN_ROW], steps])
        store = PostgresStepStore(database)

        held = await store.list_snapshots(run_id="run-a")

        assert [step.step_index for step in held] == [1, 2]
        assert ran(database, 1) == (SELECT_COMPLETE_STEPS, ("run-a",))

    async def test_an_interrupted_step_joins_only_when_the_caller_asks_for_it(self):
        database = FakeDatabase(results=[[RUN_ROW], NO_ROWS])
        store = PostgresStepStore(database)

        await store.list_snapshots(run_id="run-a", include_interrupted=True)

        assert ran(database, 1) == (SELECT_STEPS, ("run-a",))

    async def test_a_run_nothing_registered_holds_no_step(self):
        database = FakeDatabase(results=[NO_ROWS])
        store = PostgresStepStore(database)

        assert await store.list_snapshots(run_id="run-a") == []


class TestColumnsThatCarryAVocabulary:
    async def test_a_foreign_snapshot_state_is_refused_at_load(self):
        step_row: dict[str, object] = {"step_index": 1, "at": AT, "state": "bogus", "entries": []}
        database = FakeDatabase(results=[[RUN_ROW], [step_row]])
        store = PostgresStepStore(database)

        with pytest.raises(ValidationError):
            await store.latest_snapshot(run_id="run-a", include_interrupted=True)

    async def test_a_foreign_event_kind_is_refused_at_load(self):
        database = FakeDatabase(results=[[{**EVENT_ROW, "kind": "bogus"}]])
        store = PostgresStepStore(database)

        with pytest.raises(ValidationError):
            await store.list_events(run_id="run-a")

    async def test_a_foreign_tool_effect_status_is_refused_at_load(self):
        database = FakeDatabase(results=[[{**TOOL_EFFECT_ROW, "status": "bogus"}]])
        store = PostgresStepStore(database)

        with pytest.raises(ValidationError):
            await store.get_tool_effect(run_id="run-a", tool_call_id="call_1")


class TestAMissingEntryRefusesTheSnapshot:
    async def test_a_form_row_deleted_after_the_step_was_recorded_stops_the_read(self):
        step_row = {"seq": 0, "step_index": 1, "at": AT, "state": "complete", "entries": [0, 0, 0, 1, 0, 0]}
        remaining_piece = {"ordinal": 0, "form": 0, "version": 0, "content": {"kind": "request"}, "stamps": {}}
        database = FakeDatabase(results=[[RUN_ROW], [step_row], [remaining_piece]])
        store = PostgresStepStore(database)

        with pytest.raises(ValueError, match=r"run 'run-a' seq 0: missing message ordinal 1 form 0 version 0"):
            await store.latest_snapshot(run_id="run-a")


class TestDeleteConversation:
    async def test_deletes_the_continuations_then_the_runs_and_the_messages_and_lets_the_schema_cascade(self):
        database = FakeDatabase()
        store = PostgresStepStore(database)

        await store.delete_conversation(CONVERSATION)

        assert database.opened.statements() == [
            DELETE_CONVERSATION_CONTINUATION_HEADS,
            DELETE_CONVERSATION_CONTINUATION_ATTEMPTS,
            DELETE_CONVERSATION_RUNS,
            DELETE_CONVERSATION_MESSAGES,
        ], "the head goes before its attempts, and both before the runs those attempts name"
        assert [arguments for _, arguments in database.opened.executed] == [(UUID(CONVERSATION),)] * 4


class TestTransactions:
    async def test_save_snapshot_writes_inside_exactly_one_transaction(self):
        database = FakeDatabase(results=EMPTY_CONVERSATION)
        store = PostgresStepStore(database)

        await store.save_snapshot(snapshot("run-a", 1, turn_messages(), conversation_id=CONVERSATION))

        assert database.opened.transactions == [(0, len(database.opened.executed))]

    async def test_delete_conversation_runs_inside_exactly_one_transaction(self):
        database = FakeDatabase()
        store = PostgresStepStore(database)

        await store.delete_conversation(CONVERSATION)

        assert database.opened.transactions == [(0, len(database.opened.executed))]
