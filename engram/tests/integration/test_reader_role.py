"""`verify_reader_role` accepts the read-only `kb_reader` role and refuses an owner, naming the setting to change.

What this pins:

* the role every migration creates passes, so a correct deployment starts;
* the owner the database was built with fails, and the refusal names the role, what it can do and the setting;
* a reader that was granted one table outside `api` fails, naming the table.
"""

from __future__ import annotations

import asyncpg
import pytest

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.exceptions import ReaderRoleOverprivilegedError
from engram.retrieval.infrastructure.gateway import READER_SETTING, verify_reader_role
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
