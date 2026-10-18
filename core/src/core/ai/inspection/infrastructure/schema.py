"""The inspection's DDL, shipped with the package and applied by whoever owns the database."""

from importlib.resources import files
from typing import Final

from core.ai.history.contracts import Database

INSPECTION_SCHEMA: Final[str] = (files("core.ai.inspection.infrastructure") / "schema.sql").read_text(encoding="utf-8")
"""The cursors, leads and findings tables and their indexes as one script."""


async def create_inspection_schema(database: Database) -> None:
    """Create the inspection's tables and indexes, skipping every one that is already there.

    Args:
        database: The database to create them in.
    """
    async with database.acquire() as connection, connection.transaction():
        await connection.execute(INSPECTION_SCHEMA)
