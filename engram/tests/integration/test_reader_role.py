"""`verify_reader_role` accepts the read-only `kb_reader` role and refuses an owner, naming the setting to change.

What this pins:

* The role every migration creates passes, so a correct deployment starts.
* The owner the database was built with fails, and the refusal names the role, what it can do and the setting.
* A reader that was granted one table outside `api` fails, naming the table.
* The same check made on every new connection lets the reader read and refuses the owner's first read.
"""

from __future__ import annotations

import asyncpg
import pytest

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.exceptions import ReaderRoleOverprivilegedError
from engram.retrieval.infrastructure.gateway import READER_SETTING, verify_reader_role, verify_reader_role_on_connect
from tests.integration.test_migrations import _asyncpg_dsn


async def _verify(url: str) -> None:
    """Run the check over a fresh engine on `url`."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await verify_reader_role(engine)
    finally:
        await engine.dispose()


async def test_the_read_only_role_passes(reader_url: str):
    await _verify(reader_url)


async def test_the_owner_is_refused_with_the_setting_to_change(migrated: str):
    with pytest.raises(ReaderRoleOverprivilegedError) as refused:
        await _verify(migrated)

    message = str(refused.value)
    assert "'kb'" in message
    assert "not read-only" in message
    assert "superuser" in message
    assert READER_SETTING in message


async def test_a_reader_granted_a_table_outside_api_is_refused_naming_it(migrated: str, reader_url: str):
    admin = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await admin.execute("GRANT USAGE ON SCHEMA kb TO kb_reader")
        await admin.execute("GRANT SELECT ON kb.pages TO kb_reader")
    finally:
        await admin.close()

    with pytest.raises(ReaderRoleOverprivilegedError, match=r"kb\.pages outside api"):
        await _verify(reader_url)


async def _read_through_checked_connection(url: str) -> int:
    """Read one value over a fresh engine on `url` whose every new connection checks its role."""
    engine = create_async_engine(url, poolclass=NullPool)
    verify_reader_role_on_connect(engine)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT 1"))).scalar_one()
    finally:
        await engine.dispose()


async def test_a_connection_checked_on_connect_lets_the_reader_read(reader_url: str):
    assert await _read_through_checked_connection(reader_url) == 1


async def test_a_connection_checked_on_connect_refuses_the_owner_before_its_first_read(migrated: str):
    with pytest.raises(ReaderRoleOverprivilegedError, match=READER_SETTING):
        await _read_through_checked_connection(migrated)
