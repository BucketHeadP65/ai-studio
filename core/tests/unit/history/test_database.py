"""asyncpg is what the record's ports describe, with nothing between them but the pool's own adapter.

`satisfies_the_connection_port` and `satisfies_the_step_store_port` are never called: they are typed
assignments and signatures, and pyright is what runs them. The `store` assignment in
`TestTheStoreIsWhatThePortAsksFor` is the same kind of proof as the driver's, for `PostgresStepStore`
against pydantic-ai-harness's `StepStore` port.
"""

import asyncpg

from pydantic_ai_harness.step_persistence import StepStore

from core.ai.history.contracts import Connection, Database, PoolDatabase, PostgresStepStore
from tests.support.fakes.database import FakeDatabase


def satisfies_the_connection_port(connection: asyncpg.Connection) -> Connection:
    """An asyncpg connection is the connection the record's statements run on."""
    return connection


def satisfies_the_step_store_port(store: StepStore) -> StepStore:
    """A `PostgresStepStore` is the `StepStore` pydantic-ai-harness's step-persistence capability asks for."""
    return store


class TestTheDriverIsWhatTheRecordAsksFor:
    async def test_a_pool_behind_the_adapter_is_the_records_database(self):
        pool = asyncpg.create_pool("postgresql://nowhere/none")

        database: Database = PoolDatabase(pool)

        assert database is not None


class TestTheStoreIsWhatThePortAsksFor:
    async def test_postgres_step_store_is_a_step_store(self):
        database: Database = FakeDatabase()

        store: StepStore = PostgresStepStore(database)

        assert store is not None
