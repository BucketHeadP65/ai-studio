"""The coordination layer against a migrated Postgres.

Proves what the unit tier cannot: the migration matches core's schema column for column, two
workers never claim the same job, the live-key index refuses a duplicate across connections, a
wake-up inside a rolled-back transaction never reaches a listener, and the unit of work writes
events and deliveries in the same transaction as its commit. Skips without Docker like its siblings.
"""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta

import pytest

from core.cqrs.contracts import Event, JobPolicy, PostgresJobs, PostgresOutbox, PostgresWake
from core.cqrs.infrastructure import schema
from core.cqrs.infrastructure.postgres import WAKE_CHANNEL, notify_wake
from core.db import Listener, async_dsn
from pydantic import BaseModel
from sqlalchemy import NullPool, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from engram.indexing.contracts import SqlAlchemyKnowledgeBaseUnitOfWork


class _Payload(BaseModel):
    knowledge_base: str


class _Happened(Event):
    knowledge_base: str = "research"


@pytest.fixture
async def engine(migrated: str):
    engine = create_async_engine(async_dsn(migrated), poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


class TestMigrationMatchesSchema:
    @pytest.mark.parametrize("table", [schema.jobs, schema.outbox, schema.event_deliveries])
    async def test_columns_and_nullability_match(self, engine: AsyncEngine, table) -> None:
        async with engine.connect() as connection:
            live = await connection.run_sync(lambda sync: inspect(sync).get_columns(table.name, schema="coordination"))
        assert {c["name"]: bool(c["nullable"]) for c in live} == {c.name: bool(c.nullable) for c in table.columns}

    async def test_the_live_key_index_exists_and_is_partial(self, engine: AsyncEngine) -> None:
        async with engine.connect() as connection:
            definition = (
                await connection.execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'coordination' AND indexname = 'ux_jobs_live_key'"))
            ).scalar_one()
        assert "UNIQUE" in definition and "WHERE" in definition


class TestJobs:
    async def test_two_workers_never_claim_the_same_job(self, engine: AsyncEngine) -> None:
        store = PostgresJobs(engine)
        for i in range(20):
            await store.enqueue("a", str(i), _Payload(knowledge_base=str(i)), policy=JobPolicy())
        claimed: list[int] = []

        async def drain(worker: str) -> None:
            while (job := await store.claim(["a"], worker=worker)) is not None:
                claimed.append(job.id)
                await store.complete(job.id, result="ok")

        await asyncio.gather(drain("w1"), drain("w2"), drain("w3"))
        assert len(claimed) == 20 and len(set(claimed)) == 20

    async def test_a_live_key_is_refused_across_connections(self, engine: AsyncEngine) -> None:
        store = PostgresJobs(engine)
        first, second = await asyncio.gather(
            store.enqueue("knowledge_base.ingest", "research", _Payload(knowledge_base="research"), policy=JobPolicy()),
            store.enqueue("knowledge_base.ingest", "research", _Payload(knowledge_base="research"), policy=JobPolicy()),
        )
        assert {first.created, second.created} == {True, False}
        assert first.job_id == second.job_id

    async def test_an_expired_lease_is_taken_over(self, engine: AsyncEngine) -> None:
        store = PostgresJobs(engine)
        answer = await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy(lease_seconds=1))
        first = await store.claim(["a"], worker="dead")
        assert first is not None and first.attempts == 1
        assert await store.claim(["a"], worker="alive") is None
        await asyncio.sleep(1.2)
        taken = await store.claim(["a"], worker="alive")
        assert taken is not None and taken.id == answer.job_id and taken.locked_by == "alive" and taken.attempts == 2

    async def test_fail_with_a_retry_time_and_the_ledger(self, engine: AsyncEngine) -> None:
        store = PostgresJobs(engine)
        answer = await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        job = await store.claim(["a"], worker="w1")
        assert job is not None
        later = datetime.now(UTC) + timedelta(minutes=5)
        await store.fail(job.id, error="boom", retry_at=later)
        failed = await store.read(answer.job_id)
        assert failed is not None and failed.state == "failed" and failed.last_error == "boom"
        assert abs((failed.available_at - later).total_seconds()) < 1
        assert await store.claim(["a"], worker="w1") is None
        assert [j.id for j in await store.recent_jobs("a", "1")] == [answer.job_id]


class TestOutbox:
    async def test_append_deliver_close_round_trip(self, engine: AsyncEngine) -> None:
        outbox = PostgresOutbox(engine)
        event = _Happened(knowledge_base="a")
        await outbox.append([event])
        (row,) = await outbox.undelivered()
        assert row.event_id == event.event_id and row.event_type == "_Happened" and row.payload["knowledge_base"] == "a"
        assert await outbox.delivered(event.event_id, "qa") is False
        await outbox.record_delivery(event.event_id, "qa")
        await outbox.record_delivery(event.event_id, "qa")
        assert await outbox.delivered(event.event_id, "qa") is True
        await outbox.close(event.event_id)
        assert await outbox.undelivered() == []


class TestWake:
    async def test_a_committed_notify_wakes_and_a_rolled_back_one_does_not(self, engine: AsyncEngine, migrated: str) -> None:
        listener = Listener(migrated, WAKE_CHANNEL)
        await listener.open()
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT pg_notify(:c, '')"), {"c": WAKE_CHANNEL})
                await connection.rollback()
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.5):
                    await listener.get()
            async with engine.begin() as connection:
                await connection.execute(text("SELECT pg_notify(:c, '')"), {"c": WAKE_CHANNEL})
            async with asyncio.timeout(2):
                assert await listener.get() == ""
        finally:
            await listener.close()

    async def test_the_postgres_wake_returns_on_a_notification_and_on_timeout(self, engine: AsyncEngine, migrated: str) -> None:
        wake = PostgresWake(migrated)
        try:
            started = asyncio.get_running_loop().time()
            await wake.wait(0.2)
            assert asyncio.get_running_loop().time() - started >= 0.2
            async with engine.begin() as connection:
                await connection.execute(text("SELECT pg_notify(:c, '')"), {"c": WAKE_CHANNEL})
            async with asyncio.timeout(2):
                await wake.wait(10)
        finally:
            await wake.close()


class TestUnitOfWorkFlush:
    async def test_recorded_events_and_deliveries_commit_with_the_work(self, engine: AsyncEngine) -> None:
        event = _Happened()
        async with SqlAlchemyKnowledgeBaseUnitOfWork(engine) as uow:
            uow.record(event)
            uow.record_delivery(event.event_id, "qa_generation")
            await uow.commit()
        outbox = PostgresOutbox(engine)
        assert [row.event_id for row in await outbox.undelivered()] == [event.event_id]
        assert await outbox.delivered(event.event_id, "qa_generation") is True

    async def test_a_rollback_writes_nothing(self, engine: AsyncEngine) -> None:
        event = _Happened()
        async with SqlAlchemyKnowledgeBaseUnitOfWork(engine) as uow:
            uow.record(event)
        assert await PostgresOutbox(engine).undelivered() == []

    async def test_notify_wake_rides_the_transaction(self, engine: AsyncEngine, migrated: str) -> None:
        listener = Listener(migrated, WAKE_CHANNEL)
        await listener.open()
        try:
            async with SqlAlchemyKnowledgeBaseUnitOfWork(engine) as uow:
                await notify_wake(uow.session)
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.5):
                    await listener.get()
        finally:
            await listener.close()
