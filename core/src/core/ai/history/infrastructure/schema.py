"""The record's DDL, shipped with the record and applied by whoever owns the database."""

from importlib.resources import files
from typing import Final

from core.ai.history.internal_contracts import Database

RECORD_SCHEMA: Final[str] = (files("core.ai.history.infrastructure") / "schema.sql").read_text(encoding="utf-8")
"""Every table and index of the record as one script."""


async def create_record_schema(database: Database) -> None:
    """Create the record's tables and indexes, skipping every one that is already there.

    The application creates its own conversations table and the foreign keys to it afterwards.

    Args:
        database: The database to create them in.
    """
    async with database.acquire() as connection, connection.transaction():
        await connection.execute(RECORD_SCHEMA)
