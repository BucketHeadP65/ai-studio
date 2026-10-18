"""`AdminHitSources` reads which ids name a page, and each page's source document, from `kb.pages` on a migrated database."""

from __future__ import annotations

import asyncpg

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine

from engram.evals.scoring.contracts import AdminHitSources
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway


async def _seed(url: str) -> None:
    connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await connection.execute(
            "INSERT INTO kb.sources_raw (collection, source_id, provider, provider_key, title, source_hash)"
            " VALUES ('scifact', 'd4', 'beir', 'd4', 'Doc four', 'h')"
        )
        for page_id, source_id in (("dg.linked", "d4"), ("dg.unlinked", None)):
            await connection.execute(
                "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash, source_id)"
                " VALUES ($1, 'scifact', 'digest', $1, $1, 'active', $1, $1, $2)",
                page_id,
                source_id,
            )
    finally:
        await connection.close()


async def test_a_page_s_source_is_read_and_a_page_with_none_is_left_out(migrated: str) -> None:
    await _seed(migrated)
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        sources = AdminHitSources(ReadOnlySqlGateway(engine))

        found = await sources.source_ids("scifact", ["dg.linked", "dg.unlinked", "dg.missing"])
        other_collection = await sources.source_ids("nfcorpus", ["dg.linked"])
    finally:
        await engine.dispose()

    assert found == {"dg.linked": "d4"}
    assert other_collection == {}


async def test_a_page_with_or_without_a_source_is_told_from_a_source_id(migrated: str) -> None:
    await _seed(migrated)
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        sources = AdminHitSources(ReadOnlySqlGateway(engine))

        pages = await sources.page_ids("scifact", ["dg.linked", "dg.unlinked", "d4", "dg.missing"])
        other_collection = await sources.page_ids("nfcorpus", ["dg.linked"])
    finally:
        await engine.dispose()

    assert pages == {"dg.linked", "dg.unlinked"}
    assert other_collection == frozenset()
