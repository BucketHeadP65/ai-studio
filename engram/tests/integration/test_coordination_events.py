"""The topic channel against a real Postgres.

Proves what the unit tier cannot: a real `Notifier` reaches a real `Listener` on the same channel,
a broadcaster started in one process hears what a second one announced, and `publish_in` fires only
when the caller's transaction commits. Skips without Docker like its siblings.
"""

from __future__ import annotations

import asyncio

from contextlib import suppress

from core.cqrs.contracts import Broadcaster
from core.cqrs.infrastructure.broadcaster import TOPICS_CHANNEL
from core.db import Listener, Notifier, async_dsn
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine


async def _first_topic(broadcaster: Broadcaster) -> str:
    stream = broadcaster.subscribe()
    try:
        return (await asyncio.wait_for(anext(stream), timeout=5)).topic
    finally:
        await stream.aclose()


class TestRoundTrip:
    async def test_a_notified_topic_reaches_a_listener(self, migrated: str) -> None:
        listener = Listener(migrated, TOPICS_CHANNEL)
        notifier = Notifier(migrated)
        try:
            await listener.open()
            await notifier.notify(TOPICS_CHANNEL, "knowledge_base.research")
            assert await asyncio.wait_for(listener.get(), timeout=5) == "knowledge_base.research"
        finally:
            await listener.close()
            await notifier.close()

    async def test_one_broadcaster_hears_another(self, migrated: str) -> None:
        reader = Broadcaster(migrated)
        writer = Broadcaster(migrated)
        try:
            await reader.start()
            await writer.start()
            heard = asyncio.create_task(_first_topic(reader))
            await asyncio.sleep(0.1)
            writer.publish("schedules")
            assert await asyncio.wait_for(heard, timeout=5) == "schedules"
        finally:
            await reader.stop()
            await writer.stop()

    async def test_the_same_topic_twice_in_one_window_arrives_once(self, migrated: str) -> None:
        reader = Broadcaster(migrated)
        writer = Broadcaster(migrated)
        try:
            await reader.start()
            await writer.start()
            stream = reader.subscribe()
            first = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.1)
            for _ in range(4):
                writer.publish("jobs")
            assert (await asyncio.wait_for(first, timeout=5)).topic == "jobs"
            second = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.5)
            assert not second.done()
            second.cancel()
            with suppress(asyncio.CancelledError):
                await second
            await stream.aclose()
        finally:
            await reader.stop()
            await writer.stop()


class TestTransactional:
    async def test_publish_in_fires_on_commit_and_never_on_a_rollback(self, migrated: str) -> None:
        engine = create_async_engine(async_dsn(migrated), poolclass=NullPool)
        listener = Listener(migrated, TOPICS_CHANNEL)
        broadcaster = Broadcaster(migrated)
        try:
            await listener.open()
            async with engine.connect() as connection:
                await broadcaster.publish_in(connection, "knowledge_base.rolled-back")
                await connection.rollback()
            assert listener.drain() == 0
            async with engine.connect() as connection:
                await broadcaster.publish_in(connection, "knowledge_base.committed")
                await connection.commit()
            assert await asyncio.wait_for(listener.get(), timeout=5) == "knowledge_base.committed"
        finally:
            await listener.close()
            await broadcaster.stop()
            await engine.dispose()
