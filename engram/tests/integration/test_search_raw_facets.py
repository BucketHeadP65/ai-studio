"""`api.search` keeps a raw chunk under `facets` or a date range only when a current page made from its source passes them.

Every test seeds one collection with four sources, each with one raw chunk carrying the same made-up term:

* `src.kept` has a live page carrying `shade: teal`, dated September 2026.
* `src.other` has a live page carrying `shade: amber`, dated October 2026.
* `src.bare` has no page at all.
* `src.replaced` has only a superseded page, carrying `shade: teal` and dated September 2026.

The keyword leg matches every chunk, so the filter alone does the narrowing. One step down puts back the search
under which a date range drops every raw chunk and `facets` drops none, with its comment. Runs against the
`migrated` fixture and skips when Docker is unavailable.
"""

from __future__ import annotations

import json

from datetime import date

import asyncpg

from engram.testing.infrastructure.knowledge_base_db import run_downgrade, run_upgrade_head
from tests.integration.test_migrations import _asyncpg_dsn, _load_migration_module

_TERM = "facetedfrobnicator"
_COLLECTION = "faceted"


async def _source(connection: asyncpg.Connection, source_id: str) -> None:
    """One mirror and its one raw chunk carrying `_TERM`."""
    await connection.execute(
        "INSERT INTO kb.sources_raw (collection, source_id, provider, provider_key, title, source_hash)"
        " VALUES ($1, $2, 'test', $2, $2, $2)",
        _COLLECTION,
        source_id,
    )
    await connection.execute(
        "INSERT INTO kb.raw_chunks (collection, id, source_id, seq, body) VALUES ($1, $2, $3, 0, $4)",
        _COLLECTION,
        f"{source_id}#0",
        source_id,
        f"{_TERM} in a raw chunk of {source_id}",
    )


async def _page(
    connection: asyncpg.Connection, page_id: str, *, source_id: str, shade: str, page_date: date, superseded_by: str | None = None
) -> None:
    """One page made from `source_id`, carrying one `shade` value, dated `page_date`."""
    await connection.execute(
        "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash, page_date, facets, source_id, superseded_by)"
        " VALUES ($1, $2, 'digest', $3, $3, 'verified', $4, $5, $6, $7::jsonb, $8, $9)",
        page_id,
        _COLLECTION,
        page_id.split(".", 1)[-1],
        f"a digest of {source_id}",
        f"h-{page_id}",
        page_date,
        json.dumps({"shade": [shade]}),
        source_id,
        superseded_by,
    )


async def _seed(connection: asyncpg.Connection) -> None:
    """The four sources, their raw chunks and the three pages made from them."""
    for source_id in ("src.kept", "src.other", "src.bare", "src.replaced"):
        await _source(connection, source_id)
    await _page(connection, "digest.kept", source_id="src.kept", shade="teal", page_date=date(2026, 9, 15))
    await _page(connection, "digest.other", source_id="src.other", shade="amber", page_date=date(2026, 10, 1))
    await _page(connection, "digest.replaced", source_id="src.replaced", shade="teal", page_date=date(2026, 9, 15), superseded_by="digest.kept")


_RAW_SEARCH = "SELECT id FROM api.search($1, k => 20, level => 'raw', facets => $2::jsonb, collection => $3, since => $4, until => $5)"
_COMMENT = (
    "SELECT obj_description(p.oid, 'pg_proc') FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE n.nspname = 'api' AND p.proname = 'search'"
)


async def _raw_ids(
    connection: asyncpg.Connection, *, facets: dict[str, list[str]] | None = None, since: date | None = None, until: date | None = None
) -> set[str]:
    """The ids of the raw chunks one `api.search` call answers over the seeded collection."""
    rows = await connection.fetch(_RAW_SEARCH, _TERM, json.dumps(facets or {}), _COLLECTION, since, until)
    return {row["id"] for row in rows}


async def _raw_hits(url: str, *, facets: dict[str, list[str]] | None = None, since: date | None = None, until: date | None = None) -> set[str]:
    """Seed the collection, then answer the ids of the raw chunks one `api.search` call returns."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        await _seed(connection)
        return await _raw_ids(connection, facets=facets, since=since, until=until)
    finally:
        await connection.close()


class TestRawChunksUnderFacets:
    async def test_only_the_raw_chunk_of_a_source_whose_current_page_carries_the_value_is_kept(self, migrated: str) -> None:
        assert await _raw_hits(migrated, facets={"shade": ["teal"]}) == {"src.kept#0"}

    async def test_any_value_of_one_key_keeps_the_raw_chunk_of_each_matching_source(self, migrated: str) -> None:
        assert await _raw_hits(migrated, facets={"shade": ["teal", "amber"]}) == {"src.kept#0", "src.other#0"}

    async def test_a_value_no_page_carries_keeps_no_raw_chunk(self, migrated: str) -> None:
        assert await _raw_hits(migrated, facets={"shade": ["violet"]}) == set()


class TestRawChunksUnderADateRange:
    async def test_only_the_raw_chunk_of_a_source_whose_current_page_falls_in_the_range_is_kept(self, migrated: str) -> None:
        assert await _raw_hits(migrated, since=date(2026, 9, 1), until=date(2026, 9, 30)) == {"src.kept#0"}

    async def test_an_open_upper_bound_keeps_every_source_with_a_current_page_since(self, migrated: str) -> None:
        assert await _raw_hits(migrated, since=date(2026, 9, 1)) == {"src.kept#0", "src.other#0"}

    async def test_a_source_whose_page_passes_both_facets_and_the_range_keeps_its_raw_chunk(self, migrated: str) -> None:
        assert await _raw_hits(migrated, facets={"shade": ["amber"]}, since=date(2026, 10, 1)) == {"src.other#0"}

    async def test_a_source_whose_page_passes_facets_but_not_the_range_loses_its_raw_chunk(self, migrated: str) -> None:
        assert await _raw_hits(migrated, facets={"shade": ["teal"]}, since=date(2026, 10, 1)) == set()


class TestRawChunksUnfiltered:
    async def test_no_facets_and_no_range_keep_every_raw_chunk_as_before(self, migrated: str) -> None:
        assert await _raw_hits(migrated) == {"src.kept#0", "src.other#0", "src.bare#0", "src.replaced#0"}


class TestOneStepDown:
    async def test_the_earlier_search_and_its_comment_come_back(self, migrated: str) -> None:
        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            await _seed(connection)
        finally:
            await connection.close()

        run_downgrade(migrated, "0064")
        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            dated = await _raw_ids(connection, since=date(2026, 9, 1))
            faceted = await _raw_ids(connection, facets={"shade": ["teal"]})
            comment = await connection.fetchval(_COMMENT)
        finally:
            await connection.close()
        run_upgrade_head(migrated)

        assert dated == set()
        assert faceted == {"src.kept#0", "src.other#0", "src.bare#0", "src.replaced#0"}
        assert comment == _load_migration_module("0063")._COMMENT_TEXT

    async def test_the_comment_at_head_says_when_a_raw_chunk_passes(self, migrated: str) -> None:
        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            comment = str(await connection.fetchval(_COMMENT))
        finally:
            await connection.close()

        assert "A raw chunk passes 'facets', 'since' and 'until' only when a page made from its source passes them" in comment
        assert "every raw chunk drop out" not in comment
