"""`api.search`'s `since` and `until` keep pages whose page date falls in the range, inclusive, before ranking.

Every test seeds the same knowledge base: three dated pages either side of September 2026, one page
with no page date and a section under it, and one raw source chunk. All of them carry the same
made-up term, so the keyword leg matches every one and the date range alone does the narrowing. No
page carries a `created` date, so the page date alone decides. Runs against the `migrated` fixture and
skips when Docker is unavailable.
"""

from __future__ import annotations

from datetime import date

import asyncpg

from tests.integration.test_migrations import _asyncpg_dsn

_TERM = "datedfrobnicator"
_COLLECTION = "dated"


async def _insert_page(connection: asyncpg.Connection, page_id: str, page_date: date | None) -> None:
    """One minimal `kb.pages` row carrying the shared term, dated `page_date`."""
    await connection.execute(
        "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash, page_date)"
        " VALUES ($1, $2, 'paper', $3, $3, 'verified', $4, $5, $6)",
        page_id,
        _COLLECTION,
        page_id.split(".", 1)[-1],
        f"{_TERM} notes for {page_id}",
        f"h-{page_id}",
        page_date,
    )


async def _seed(connection: asyncpg.Connection) -> None:
    """The five pages, a section under the undated one, and a raw source chunk, all carrying `_TERM`."""
    await _insert_page(connection, "paper.august", date(2026, 8, 31))
    await _insert_page(connection, "paper.september", date(2026, 9, 15))
    await _insert_page(connection, "paper.october", date(2026, 10, 1))
    await _insert_page(connection, "paper.undated", None)
    await connection.execute(
        "INSERT INTO kb.sections (collection, id, page_id, heading, path, ord, body, body_hash)"
        " VALUES ($1, 'paper.undated#notes', 'paper.undated', 'Notes', 'notes', 0, $2, 'h-notes')",
        _COLLECTION,
        f"{_TERM} in a section of an undated page",
    )
    await connection.execute(
        "INSERT INTO kb.sources_raw (collection, source_id, provider, provider_key, title, source_hash)"
        " VALUES ($1, 'src-1', 'test', 'k-1', 'A raw source', 'h-src')",
        _COLLECTION,
    )
    await connection.execute(
        "INSERT INTO kb.raw_chunks (collection, id, source_id, seq, body) VALUES ($1, 'src-1#0', 'src-1', 0, $2)",
        _COLLECTION,
        f"{_TERM} in a raw chunk",
    )


async def _search(url: str, *, since: date | None = None, until: date | None = None) -> list[tuple[str, str | None]]:
    """Every `(hit_level, page_id)` one `api.search` call answers over the seeded knowledge base."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        await _seed(connection)
        rows = await connection.fetch(
            "SELECT hit_level, page_id FROM api.search($1, k => 20, collection => $2, since => $3, until => $4)",
            _TERM,
            _COLLECTION,
            since,
            until,
        )
    finally:
        await connection.close()
    return [(row["hit_level"], row["page_id"]) for row in rows]


class TestSearchDateRange:
    async def test_a_month_keeps_only_the_pages_dated_in_it(self, migrated: str) -> None:
        hits = await _search(migrated, since=date(2026, 9, 1), until=date(2026, 9, 30))

        assert {page_id for _, page_id in hits} == {"paper.september"}

    async def test_both_bounds_are_inclusive(self, migrated: str) -> None:
        hits = await _search(migrated, since=date(2026, 8, 31), until=date(2026, 10, 1))

        assert {page_id for _, page_id in hits} == {"paper.august", "paper.september", "paper.october"}

    async def test_an_open_upper_bound_keeps_everything_since(self, migrated: str) -> None:
        hits = await _search(migrated, since=date(2026, 9, 1))

        assert {page_id for _, page_id in hits} == {"paper.september", "paper.october"}

    async def test_an_undated_page_its_sections_and_raw_chunks_drop_out_of_any_range(self, migrated: str) -> None:
        hits = await _search(migrated, until=date(2030, 12, 31))

        assert ("card", "paper.undated") not in hits
        assert ("section", "paper.undated") not in hits
        assert [level for level, _ in hits if level == "raw"] == []

    async def test_no_range_ranks_over_every_tier_as_before(self, migrated: str) -> None:
        hits = await _search(migrated)

        assert {page_id for _, page_id in hits} >= {"paper.august", "paper.september", "paper.october", "paper.undated"}
        assert ("section", "paper.undated") in hits
        assert "raw" in {level for level, _ in hits}
