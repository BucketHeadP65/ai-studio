"""A search date range reads each page's page date, the day the schema's `retrieval.date_field` names, never `created`.

The schema here names `observed_on` as its date field. One page written on 2026-05-04 records an observation of
2019-03-14 and is made from one source mirror, whose raw chunk carries the same made-up term as the page. The knowledge
base is indexed into a freshly migrated database, and `api.search` keeps the page and its source's raw chunk, one hit
with the other folded under it, under a range around the observed day and neither under a range around the written day, while `kb.pages.created` keeps the
written day. The revision that adds the column fills it from `created` for every page already indexed, and one step
down puts back the search that read `created`. Runs against the `migrated` fixture and skips when Docker is unavailable.
"""

from __future__ import annotations

import json

from datetime import date
from typing import TYPE_CHECKING

import asyncpg

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.init.contracts import scaffold
from engram.kb.contracts import CanonicalSection, FrontmatterFieldSpec, KnowledgeBaseSchema, PageTypeSpec, SourcesSpec
from engram.kb.domain.schema import EmbedStrategiesSpec, RetrievalSpec
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.infrastructure.knowledge_base_db import run_downgrade, run_upgrade_head
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from tests.integration.test_migrations import _asyncpg_dsn, _load_migration_module

if TYPE_CHECKING:
    from pathlib import Path

_COLLECTION = "logbook"
_TERM = "pagedatefrobnicator"
_PAGE_ID = "record.heron-count"
_WRITTEN = date(2026, 5, 4)
_OBSERVED = date(2019, 3, 14)

_PAGE = f"""---
id: {_PAGE_ID}
type: record
title: Heron count
status: draft
created: {_WRITTEN.isoformat()}
observed_on: {_OBSERVED.isoformat()}
source_key: field-notes
---

## Notes

The {_TERM} heron count from the marsh.
"""

_MIRROR = f"""---
source_key: field-notes
source_hash: h-field-notes
provider: test
title: Field notes
---

# Field notes

The {_TERM} tally sheet from the marsh.
"""

_SEARCH = "SELECT hit_level, page_id FROM api.search($1, k => 20, collection => $2, since => $3, until => $4)"
_UNITS_SEARCH = "SELECT id, hit_level, folded_units FROM api.search($1, k => 20, collection => $2, since => $3, until => $4)"
_COMMENT = (
    "SELECT obj_description(p.oid, 'pg_proc') FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE n.nspname = 'api' AND p.proname = 'search'"
)


def _spec() -> KnowledgeBaseSchema:
    """One page type whose pages name a source and an observed day, the day the schema dates each page by."""
    return KnowledgeBaseSchema(
        slug=_COLLECTION,
        page_types=(
            PageTypeSpec(
                page_type="record",
                id_prefix="record.",
                folder="Wiki/Records",
                canonical_sections=(CanonicalSection(slug="notes"),),
                frontmatter_fields=(
                    FrontmatterFieldSpec(name="observed_on", type="scalar"),
                    FrontmatterFieldSpec(name="source_key", type="scalar"),
                ),
            ),
        ),
        sources=SourcesSpec(key_field="source_key"),
        retrieval=RetrievalSpec(date_field="observed_on"),
        embed_strategies=EmbedStrategiesSpec(active=("page-card", "section", "source-chunk")),
    )


def _knowledge_base(root: Path, spec: KnowledgeBaseSchema) -> Path:
    """Write the page, its source mirror, the vocabulary the schema seeds and the orientation stubs under `root`."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema(spec)
    for relative, body in scaffold(spec).items():
        if relative.startswith("_meta/vocabulary/"):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(body, encoding="utf-8")
    builder.add_unit_document("Wiki/Records", "heron-count", _PAGE)
    builder.add_source_mirror("field-notes", _MIRROR)
    return root


async def _index(root: Path, migrated: str) -> None:
    """Run one full index of the knowledge base at `root` into the migrated database."""
    spec = _spec()
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        handler = IndexKnowledgeBaseHandler(
            FilesystemKnowledgeBaseReader(_knowledge_base(root, spec), note_folders=spec.note_folders),
            FakeEmbedding(),
            lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine),
            FakeValidationReportSink(),
            spec=spec,
        )
        await handler.handle(IndexKnowledgeBase(collection=_COLLECTION, embed=False))
    finally:
        await engine.dispose()


async def _hits(url: str, *, since: date, until: date) -> set[tuple[str, str | None]]:
    """Every `(hit_level, page_id)` one dated `api.search` call answers over the indexed knowledge base."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch(_SEARCH, _TERM, _COLLECTION, since, until)
    finally:
        await connection.close()
    return {(row["hit_level"], row["page_id"]) for row in rows}


async def _units(url: str, *, since: date, until: date) -> set[tuple[str, str]]:
    """Every `(hit_level, id)` one dated `api.search` call answers at head, each hit's folded units included."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch(_UNITS_SEARCH, _TERM, _COLLECTION, since, until)
    finally:
        await connection.close()
    hits = {(row["hit_level"], row["id"]) for row in rows}
    folded = {(unit["hit_level"], unit["id"]) for row in rows for unit in json.loads(row["folded_units"])}
    return hits | folded


async def _dates(url: str) -> tuple[date | None, date | None]:
    """The indexed page's `created` and `page_date`."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        row = await connection.fetchrow("SELECT created, page_date FROM kb.pages WHERE collection = $1 AND id = $2", _COLLECTION, _PAGE_ID)
    finally:
        await connection.close()
    assert row is not None
    return row["created"], row["page_date"]


class TestARangeReadsThePageDate:
    async def test_a_range_around_the_named_fields_day_keeps_the_page_and_its_sources_raw_chunk(self, tmp_path: Path, migrated: str) -> None:
        await _index(tmp_path, migrated)

        units = await _units(migrated, since=date(2019, 3, 1), until=date(2019, 3, 31))

        assert {unit_id.partition("#")[0] for level, unit_id in units if level != "raw"} == {_PAGE_ID}
        assert "raw" in {level for level, _ in units}

    async def test_a_range_around_the_day_the_page_was_written_keeps_neither(self, tmp_path: Path, migrated: str) -> None:
        await _index(tmp_path, migrated)

        hits = await _hits(migrated, since=date(2026, 5, 1), until=date(2026, 5, 31))

        assert hits == set()

    async def test_created_keeps_the_day_the_page_was_written(self, tmp_path: Path, migrated: str) -> None:
        await _index(tmp_path, migrated)

        assert await _dates(migrated) == (_WRITTEN, _OBSERVED)


async def _insert_written_page(url: str) -> None:
    """One page carrying the shared term, written on `_WRITTEN`, inserted straight into `kb.pages`."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        await connection.execute(
            "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash, created)"
            " VALUES ($1, $2, 'record', 'heron-count', 'Heron count', 'draft', $3, 'h-page', $4)",
            _PAGE_ID,
            _COLLECTION,
            f"The {_TERM} heron count",
            _WRITTEN,
        )
    finally:
        await connection.close()


async def _page_date_column_exists(url: str) -> bool:
    """Whether `kb.pages` carries a `page_date` column."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        found = await connection.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'kb' AND table_name = 'pages' AND column_name = 'page_date'"
        )
    finally:
        await connection.close()
    return found == 1


async def _comment(url: str) -> str:
    """The comment `api.search` carries."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return str(await connection.fetchval(_COMMENT))
    finally:
        await connection.close()


class TestTheRevision:
    async def test_upgrade_fills_the_page_date_of_every_page_already_indexed_from_created(self, migrated: str) -> None:
        run_downgrade(migrated, "0065")
        await _insert_written_page(migrated)

        run_upgrade_head(migrated)

        assert await _dates(migrated) == (_WRITTEN, _WRITTEN)

    async def test_one_step_down_drops_the_column_and_puts_back_the_search_that_read_created(self, migrated: str) -> None:
        run_downgrade(migrated, "0065")
        await _insert_written_page(migrated)

        hits = await _hits(migrated, since=date(2026, 5, 1), until=date(2026, 5, 31))
        exists = await _page_date_column_exists(migrated)
        comment = await _comment(migrated)
        run_upgrade_head(migrated)

        assert hits == {("card", _PAGE_ID)}
        assert exists is False
        assert comment == _load_migration_module("0065")._COMMENT_TEXT

    async def test_the_comment_at_head_says_a_range_reads_the_page_date(self, migrated: str) -> None:
        comment = await _comment(migrated)

        assert "page date" in comment
        assert "created date" not in comment
