"""The record in the sessions database: a run round-trips through the port, and the projection cannot see it.

Both pools are real here, opened the way a deployment opens them, because what is under test is
which tables each one reaches: the record and the projection each own a table called `messages`,
and only the pool a statement runs on decides which of the two it is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from core.ai.history.contracts import PoolDatabase, PostgresStepStore
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord

from harness.adapters.store import RECORD_NAMESPACE
from tests.integration.conftest import as_asyncpg_dsn

if TYPE_CHECKING:
    from harness.adapters.store import RecordDatabase
    from harness.features.sessions.infrastructure.postgres import PostgresProjectionPool


async def _a_run_of_one_message(store: PostgresStepStore, *, conversation_id: str, said: str) -> str:
    """Register one run of this conversation and save its first step, the way a settled turn does.

    The run is named after its conversation, so two tests sharing the one container never register
    the same run twice.

    Returns:
        The run's id.
    """
    run_id = f"run-{conversation_id}"
    await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id))
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id=run_id,
            step_index=0,
            messages=[ModelRequest(parts=[UserPromptPart(content=said)])],
            conversation_id=conversation_id,
        )
    )
    return run_id


async def test_the_schema_applies_and_a_run_round_trips(record_database: RecordDatabase) -> None:
    """What one snapshot saved reads back whole, off the pool the record was opened on."""
    store = PostgresStepStore(PoolDatabase(record_database.pool))
    conversation_id = str(uuid4())

    run_id = await _a_run_of_one_message(store, conversation_id=conversation_id, said="the record holds this")

    snapshot = await store.latest_snapshot(run_id=run_id)
    assert snapshot is not None
    [message] = snapshot.messages
    assert isinstance(message, ModelRequest)
    [part] = message.parts
    assert isinstance(part, UserPromptPart)
    assert part.content == "the record holds this"
    assert [record.run_id for record in await store.list_runs(conversation_id=conversation_id)] == [run_id]
    assert [held.step_index for held in await store.list_snapshots(run_id=run_id)] == [0]


async def test_each_pool_resolves_unqualified_names_in_its_own_schema(
    record_database: RecordDatabase, projection_pool: PostgresProjectionPool
) -> None:
    """Each pool answers with its own schema, which is what an unqualified name in its statements resolves in.

    Both reads take a connection out of their pool and give it back, so what they report is what
    the next statement on that pool will resolve against. A schema-qualified name still reaches
    across from either pool; nothing here claims otherwise.
    """
    assert await record_database.pool.fetchval("SELECT current_schema()") == RECORD_NAMESPACE
    assert await projection_pool.fetchval("SELECT current_schema()") == "public"


async def test_the_records_messages_are_not_the_projections(
    record_database: RecordDatabase, projection_pool: PostgresProjectionPool
) -> None:
    """One statement, two pools, two tables: what the record wrote is not in the projection's rows."""
    conversation_id = str(uuid4())
    assert await record_database.pool.fetchval("SELECT 'messages'::regclass::oid") != await projection_pool.fetchval(
        "SELECT 'messages'::regclass::oid"
    )
    projected_before = await projection_pool.fetchval("SELECT count(*) FROM messages")

    await _a_run_of_one_message(PostgresStepStore(PoolDatabase(record_database.pool)), conversation_id=conversation_id, said="only the record")

    held = await record_database.pool.fetchval("SELECT count(*) FROM messages WHERE conversation_id = $1", UUID(conversation_id))
    assert held == 1
    assert await projection_pool.fetchval("SELECT count(*) FROM messages") == projected_before


def test_the_container_dsn_keeps_the_query_it_already_carried() -> None:
    """A URL that already names terms keeps them, with `sslmode` merged in rather than appended."""
    dsn = as_asyncpg_dsn("postgresql+asyncpg://kb:kb@localhost:5432/kb?application_name=probe")

    assert dsn == "postgresql://kb:kb@localhost:5432/kb?application_name=probe&sslmode=disable"
