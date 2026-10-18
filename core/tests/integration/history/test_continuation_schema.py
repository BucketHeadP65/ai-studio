"""The continuation ledger against a real Postgres: the bootstrap it ships with, and two turns racing for one line.

The bootstrap claim is the additive one: a record provisioned before these tables existed gains
exactly them, twice-applied, with every history row it already held still there. The concurrency
claim is the one a fake cannot make: two workers reaching for the same conversation at the same
predecessor, through two connections, and exactly one of them running a model call.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from asyncpg.exceptions import UniqueViolationError
from pydantic_ai_harness.step_persistence import RunRecord, ToolEffectRecord

from core.ai.history.contracts import (
    ContinuationAttempt,
    ContinuationConflictError,
    ContinuationInFlightError,
    ContinuationRef,
    ContinuationUnsafeReplayError,
    PoolDatabase,
    PostgresContinuationStore,
    PostgresStepStore,
    create_record_schema,
    rows_as,
)
from core.ai.history.infrastructure.checkpoints import checkpoint_digest
from tests.integration.conftest import TEST_SCHEMA
from tests.unit.history.conftest import snapshot, turn_messages

if TYPE_CHECKING:
    from core.ai.history.contracts import ContinuationCommit

AT = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
DROP_CONTINUATION = "DROP TABLE IF EXISTS continuation_heads, continuation_attempts CASCADE"
SELECT_TABLES = """
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = $1 AND table_name IN ('continuation_attempts', 'continuation_heads')
    ORDER BY table_name
"""
SELECT_KEY_COLUMNS = """
    SELECT held.constraint_type, columns.column_name
    FROM information_schema.table_constraints AS held
    JOIN information_schema.key_column_usage AS columns
      ON columns.constraint_schema = held.constraint_schema AND columns.constraint_name = held.constraint_name
    WHERE held.constraint_schema = $1 AND held.table_name = $2 AND held.constraint_type = ANY($3::text[])
    ORDER BY held.constraint_type, columns.ordinal_position
"""
SELECT_INDEX = """
    SELECT indexdef FROM pg_indexes
    WHERE schemaname = $1 AND tablename = 'continuation_attempts' AND indexname = 'continuation_one_started'
"""


@dataclass(frozen=True)
class TableNameRow:
    """One table `information_schema.tables` answered with."""

    table_name: str


@dataclass(frozen=True)
class KeyColumnRow:
    """One column of one key constraint, in the position the constraint declares it."""

    constraint_type: str
    column_name: str


@dataclass(frozen=True)
class IndexDefinitionRow:
    """One index as Postgres spells it back."""

    indexdef: str


@pytest.fixture
def store(database: PoolDatabase) -> PostgresStepStore:
    return PostgresStepStore(database)


@pytest.fixture
def checkpoints(database: PoolDatabase) -> PostgresContinuationStore:
    return PostgresContinuationStore(database)


@pytest.fixture
def conversation_id() -> UUID:
    return uuid4()


def an_attempt(conversation_id: UUID, *, attempt_id: str, revision: int = 0, run_id: str | None = None) -> ContinuationAttempt:
    """One claim on a line, with the deterministic run id the turn will register."""
    return ContinuationAttempt(
        conversation_id=conversation_id,
        attempt_id=attempt_id,
        expected_revision=revision,
        run_id=run_id if run_id is not None else f"{conversation_id}:{attempt_id[:16]}",
    )


async def _a_settled_turn(store: PostgresStepStore, attempt: ContinuationAttempt, *, step_index: int = 1) -> int:
    """Register the attempt's run and save it one settled step, the way `StepPersistence` would. Answers its sequence."""
    await store.register_run(
        RunRecord(run_id=attempt.run_id, conversation_id=str(attempt.conversation_id), agent_name="turn", started_at=AT)
    )
    await store.save_snapshot(
        snapshot(attempt.run_id, step_index, turn_messages(), conversation_id=str(attempt.conversation_id))
    )
    held = await store.list_snapshots(run_id=attempt.run_id)
    return len(held) - 1


class TestBootstrap:
    async def test_a_record_provisioned_before_these_tables_gains_exactly_them_and_keeps_its_history(
        self, database: PoolDatabase, store: PostgresStepStore, conversation_id: UUID
    ) -> None:
        async with database.acquire() as connection, connection.transaction():
            await connection.execute(DROP_CONTINUATION)
        run_id = f"run-before-{conversation_id}"
        await store.register_run(RunRecord(run_id=run_id, conversation_id=str(conversation_id), agent_name="older", started_at=AT))
        await store.save_snapshot(snapshot(run_id, 1, turn_messages(), conversation_id=str(conversation_id)))

        await create_record_schema(database)
        await create_record_schema(database)

        async with database.acquire() as connection:
            tables = await connection.fetch(SELECT_TABLES, TEST_SCHEMA)
            attempt_keys = await connection.fetch(SELECT_KEY_COLUMNS, TEST_SCHEMA, "continuation_attempts", ["PRIMARY KEY"])
            head_keys = await connection.fetch(SELECT_KEY_COLUMNS, TEST_SCHEMA, "continuation_heads", ["PRIMARY KEY", "FOREIGN KEY"])
            indexes = await connection.fetch(SELECT_INDEX, TEST_SCHEMA)

        assert [row.table_name for row in rows_as(TableNameRow, tables)] == ["continuation_attempts", "continuation_heads"]
        assert [row.column_name for row in rows_as(KeyColumnRow, attempt_keys)] == ["conversation_id", "attempt_id"]
        assert [(row.constraint_type, row.column_name) for row in rows_as(KeyColumnRow, head_keys)] == [
            ("FOREIGN KEY", "conversation_id"),
            ("FOREIGN KEY", "attempt_id"),
            ("PRIMARY KEY", "conversation_id"),
        ]
        defined = rows_as(IndexDefinitionRow, indexes)
        assert len(defined) == 1
        assert "status = 'started'" in defined[0].indexdef, "the one-started index is the partial one"

        kept = await store.list_snapshots(run_id=run_id)
        assert [held.messages for held in kept] == [turn_messages()], "every history row the record already held is still there"
        assert await store.get_run(run_id=run_id) is not None

    async def test_the_started_index_lets_one_attempt_be_unfinished_and_no_more(
        self, database: PoolDatabase, conversation_id: UUID
    ) -> None:
        insert = """
            INSERT INTO continuation_attempts (conversation_id, attempt_id, expected_revision, status, run_id, started_at)
            VALUES ($1, $2, 0, 'started', $3, $4)
        """
        async with database.acquire() as connection, connection.transaction():
            await connection.execute(insert, conversation_id, "a" * 64, "run-a", AT)

        with pytest.raises(UniqueViolationError, match="continuation_one_started"):
            async with database.acquire() as connection, connection.transaction():
                await connection.execute(insert, conversation_id, "b" * 64, "run-b", AT)


class TestConcurrentBegin:
    async def test_two_workers_reaching_for_one_line_admit_exactly_one_model_call(
        self, checkpoints: PostgresContinuationStore, conversation_id: UUID
    ) -> None:
        model_calls: list[str] = []

        async def turn(attempt_id: str) -> Exception | None:
            """Begin, and count a model call only where the ledger admitted one."""
            try:
                admitted = await checkpoints.begin_attempt(an_attempt(conversation_id, attempt_id=attempt_id))
            except (ContinuationConflictError, ContinuationInFlightError, ContinuationUnsafeReplayError) as refusal:
                return refusal
            assert admitted is None
            model_calls.append(attempt_id)
            return None

        outcomes = await asyncio.gather(turn("a" * 64), turn("b" * 64))

        refusals = [outcome for outcome in outcomes if outcome is not None]
        assert len(model_calls) == 1, "exactly one attempt ran a model call"
        assert len(refusals) == 1
        assert isinstance(refusals[0], ContinuationInFlightError)
        assert model_calls[0] in str(refusals[0]), "the refusal names the attempt that is holding the line"


class TestOneLineAcrossTurns:
    async def test_a_second_turn_resumes_the_first_turns_exact_checkpoint(
        self, checkpoints: PostgresContinuationStore, store: PostgresStepStore, conversation_id: UUID
    ) -> None:
        first = an_attempt(conversation_id, attempt_id="a" * 64)
        assert await checkpoints.begin_attempt(first) is None
        seq = await _a_settled_turn(store, first)
        held = await checkpoints.latest_checkpoint(run_id=first.run_id)
        assert held is not None
        commit = await checkpoints.commit_attempt(
            first, snapshot_seq=held.snapshot_seq, checkpoint_digest=held.checkpoint_digest, output={"note": "worth a look"}
        )

        assert commit.ref.revision == 1
        assert commit.ref.snapshot_seq == seq

        second = an_attempt(conversation_id, attempt_id="b" * 64, revision=1)
        assert await checkpoints.begin_attempt(second) is None
        resumed = await checkpoints.read_checkpoint(commit.ref)

        assert resumed.messages == turn_messages()
        assert resumed.conversation_id == str(conversation_id)

    async def test_a_crash_after_commit_hands_the_stored_output_back_without_running_again(
        self, checkpoints: PostgresContinuationStore, store: PostgresStepStore, conversation_id: UUID
    ) -> None:
        attempt = an_attempt(conversation_id, attempt_id="a" * 64)
        await checkpoints.begin_attempt(attempt)
        await _a_settled_turn(store, attempt)
        held = await checkpoints.latest_checkpoint(run_id=attempt.run_id)
        assert held is not None
        first = await checkpoints.commit_attempt(
            attempt, snapshot_seq=held.snapshot_seq, checkpoint_digest=held.checkpoint_digest, output={"note": "worth a look"}
        )

        redelivered: ContinuationCommit | None = await checkpoints.begin_attempt(attempt)

        assert redelivered is not None
        assert redelivered.output == {"note": "worth a look"}
        assert redelivered.ref == first.ref

    async def test_committing_the_same_attempt_twice_is_the_same_answer_twice(
        self, checkpoints: PostgresContinuationStore, store: PostgresStepStore, conversation_id: UUID
    ) -> None:
        attempt = an_attempt(conversation_id, attempt_id="a" * 64)
        await checkpoints.begin_attempt(attempt)
        await _a_settled_turn(store, attempt)
        held = await checkpoints.latest_checkpoint(run_id=attempt.run_id)
        assert held is not None

        first = await checkpoints.commit_attempt(
            attempt, snapshot_seq=held.snapshot_seq, checkpoint_digest=held.checkpoint_digest, output={"note": "a"}
        )
        again = await checkpoints.commit_attempt(
            attempt, snapshot_seq=held.snapshot_seq, checkpoint_digest=held.checkpoint_digest, output={"note": "a"}
        )

        assert again == first

    async def test_an_unresolved_tool_effect_refuses_the_commit(
        self, checkpoints: PostgresContinuationStore, store: PostgresStepStore, conversation_id: UUID
    ) -> None:
        attempt = an_attempt(conversation_id, attempt_id="a" * 64)
        await checkpoints.begin_attempt(attempt)
        await _a_settled_turn(store, attempt)
        await store.record_tool_effect(
            ToolEffectRecord(tool_call_id="call_1", tool_name="lookup_city", run_id=attempt.run_id, status="started", started_at=AT)
        )
        held = await checkpoints.latest_checkpoint(run_id=attempt.run_id)
        assert held is not None

        with pytest.raises(ContinuationUnsafeReplayError) as refusal:
            await checkpoints.commit_attempt(
                attempt, snapshot_seq=held.snapshot_seq, checkpoint_digest=held.checkpoint_digest, output={"note": "a"}
            )

        assert "lookup_city/call_1" in str(refusal.value)

    async def test_a_checkpoint_of_another_line_is_refused_rather_than_read(
        self, checkpoints: PostgresContinuationStore, store: PostgresStepStore, conversation_id: UUID
    ) -> None:
        attempt = an_attempt(conversation_id, attempt_id="a" * 64)
        await checkpoints.begin_attempt(attempt)
        await _a_settled_turn(store, attempt)
        held = await checkpoints.latest_checkpoint(run_id=attempt.run_id)
        assert held is not None
        elsewhere = ContinuationRef(
            conversation_id=uuid4(),
            revision=1,
            run_id=attempt.run_id,
            snapshot_seq=held.snapshot_seq,
            checkpoint_digest=held.checkpoint_digest,
        )

        with pytest.raises(ContinuationUnsafeReplayError):
            await checkpoints.read_checkpoint(elsewhere)

    async def test_deleting_the_conversation_takes_its_head_and_its_attempts_with_it(
        self, checkpoints: PostgresContinuationStore, store: PostgresStepStore, database: PoolDatabase, conversation_id: UUID
    ) -> None:
        attempt = an_attempt(conversation_id, attempt_id="a" * 64)
        await checkpoints.begin_attempt(attempt)
        await _a_settled_turn(store, attempt)
        held = await checkpoints.latest_checkpoint(run_id=attempt.run_id)
        assert held is not None
        await checkpoints.commit_attempt(
            attempt, snapshot_seq=held.snapshot_seq, checkpoint_digest=held.checkpoint_digest, output={"note": "a"}
        )

        await store.delete_conversation(str(conversation_id))

        async with database.acquire() as connection:
            heads = await connection.fetch("SELECT 1 FROM continuation_heads WHERE conversation_id = $1", conversation_id)
            attempts = await connection.fetch("SELECT 1 FROM continuation_attempts WHERE conversation_id = $1", conversation_id)
        assert heads == []
        assert attempts == []

    async def test_the_digest_a_commit_stores_is_the_digest_of_what_the_step_holds(
        self, checkpoints: PostgresContinuationStore, store: PostgresStepStore, conversation_id: UUID
    ) -> None:
        attempt = an_attempt(conversation_id, attempt_id="a" * 64)
        await checkpoints.begin_attempt(attempt)
        await _a_settled_turn(store, attempt)
        held = await checkpoints.latest_checkpoint(run_id=attempt.run_id)

        assert held is not None
        assert held.checkpoint_digest == checkpoint_digest(turn_messages())
