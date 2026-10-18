"""kb.unit_summaries exists with the pending-default lifecycle."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def test_unit_summaries_roundtrip(reader_url: str, migrated: str) -> None:
    engine = create_async_engine(migrated)
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO kb.unit_summaries (unit_id, unit_kind, collection, summary, model, source_hash) "
            "VALUES ('mu.x', 'page', 'workdocs', 'A summary.', 'test-model', 'abc')"))
        row = (await conn.execute(text("SELECT status FROM kb.unit_summaries WHERE unit_id = 'mu.x'"))).one()
    assert row.status == "pending"
    await engine.dispose()
