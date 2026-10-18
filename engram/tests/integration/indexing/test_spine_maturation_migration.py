"""Integration test for the 0024 spine-maturation migration's fold, round-trip, and api surface.

Drives ``alembic downgrade 0023`` against the ``migrated`` fixture's throwaway local Postgres
container (undoing 0024, back to the pre-maturation column shape), seeds rows through that OLD
shape with raw SQL (the shape a pre-0024 index would have left), then ``alembic upgrade head``
re-applies 0024 and this module asserts the fold landed: the seven scalars + five facet arrays
in ``pages.frontmatter``/``pages.facets``, the vendor-neutral identity in ``sources_raw``, and
one ``claim_qualifiers`` row per resolved qualifier target. ``api.search``'s jsonb ``facets``
parameter and ``api.lookup_value`` are exercised against the migrated data, and the down/up half
proves ``downgrade 0023`` reconstructs the old columns losslessly and a second ``upgrade head``
round-trips cleanly.

Mirrors ``test_compile_state_migration.py``'s ``_run_alembic`` pattern: alembic commands run on a
loop-free worker thread (``alembic/env.py``'s ``run_migrations_online`` calls ``asyncio.run()``,
which cannot nest inside the running ``asyncio_mode = auto`` test loop).

The whole module skips when Docker is unavailable (the ``pg`` fixture, via ``migrated``, raises
``pytest.skip``).
"""

from __future__ import annotations

import json

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import asyncpg
import pytest

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from engram.testing.infrastructure.knowledge_base_db import BED_GRAPH_BACKEND

# Mirrors `tests/conftest.py`'s `_BED_DIALECT`: the analog migrates under `pg_textsearch`, the opt-in
# ParadeDB bed under `pg_search`.
_BED_DIALECT = "pg_textsearch" if BED_GRAPH_BACKEND == "age" else "pg_search"

# `engram/` — three levels up from `tests/integration/index/`; the alembic.ini lives here.
_ENGRAM_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _ENGRAM_ROOT / "alembic.ini"
_ALEMBIC_DIR = _ENGRAM_ROOT / "alembic"


def _alembic_config(url: str) -> Config:
    """Build an alembic Config pinned to the app's ini + the throwaway DB and the bed's grammar."""
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    config.attributes["sqlalchemy_url"] = url
    config.attributes["dialect"] = _BED_DIALECT
    config.attributes["graph_backend"] = BED_GRAPH_BACKEND
    return config


def _run_alembic(url: str, revision: str, *, downgrade: bool) -> None:
    """Run ``alembic upgrade``/``downgrade`` to ``revision`` against ``url`` on a loop-free thread."""
    config = _alembic_config(url)
    run = command.downgrade if downgrade else command.upgrade
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run, config, revision).result()


def _chain_head(url: str) -> str:
    """The migration chain's head revision, read from the scripts so a new revision never stales this module."""
    head = ScriptDirectory.from_config(_alembic_config(url)).get_current_head()
    assert head is not None, "alembic script directory has no head revision"
    return head


@pytest.fixture
async def pre_maturation(migrated: str) -> str:
    """The `migrated` (head) database, downgraded to 0023 — the pre-0024 column shape."""
    _run_alembic(migrated, "0023", downgrade=True)
    return migrated


async def _conn(url: str) -> asyncpg.Connection:
    """A raw asyncpg connection with the jsonb codec registered (so `jsonb` columns decode to dicts)."""
    connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    await connection.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    return connection


async def _seed_old_shape(url: str) -> None:
    """Insert one source, three pages, and one claim through the pre-0024 column shape.

    `paper.graph-cost` cites `GRAPHKEY1` (year/venue/doi/arxiv_id on the mirror row, the seven
    scalars + two facet arrays on the page), and its one claim resolves `on_dataset` /`on_task`/
    `metric` to the other two pages — exactly the shape 0024's fold reads.
    """
    connection = await _conn(url)
    try:
        await connection.execute(
            "INSERT INTO memory.pages (id, collection, kind, slug, title, status, card_text, body_hash)"
            " VALUES ('ds.hotpotqa', 'research', 'entity', 'hotpotqa', 'HotpotQA', 'verified', 'entity: HotpotQA', 'hds')"
        )
        await connection.execute(
            "INSERT INTO memory.pages (id, collection, kind, slug, title, status, card_text, body_hash)"
            " VALUES ('tk.multihopqa', 'research', 'entity', 'multihopqa', 'Multi-hop QA', 'verified', 'entity: Multi-hop QA', 'htk')"
        )
        await connection.execute(
            "INSERT INTO memory.pages (id, collection, kind, slug, title, status, card_text, body_hash)"
            " VALUES ('mt.f1', 'research', 'entity', 'f1', 'F1', 'verified', 'entity: F1', 'hmt')"
        )
        await connection.execute(
            "INSERT INTO memory.sources_raw (zotero_key, collection, title, year, venue, doi, arxiv_id, source_hash)"
            " VALUES ('GRAPHKEY1', 'research', 'Graph Indexing Token Cost', 2025, 'EMNLP 2025',"
            " '10.18653/v1/2025.x', '2410.05779', 'g001')"
        )
        await connection.execute(
            "INSERT INTO memory.pages ("
            "  id, collection, kind, slug, title, status, card_text, body_hash,"
            "  entity_type, paper_type, year, venue, citekey, zotero_key, maturity, question_status,"
            "  tasks, domains, stages"
            ") VALUES ("
            "  'paper.graph-cost', 'research', 'paper', 'graph-cost', 'Graph Indexing Token Cost widget-frobnicator',"
            "  'synthesized', 'paper: Graph Indexing Token Cost widget-frobnicator — retrieval indexing costs', 'hp1',"
            "  NULL, 'method', 2025, 'EMNLP 2025', 'guo2025graph', 'GRAPHKEY1', NULL, NULL,"
            "  ARRAY['retrieval'], ARRAY['nlp'], ARRAY['indexing']"
            ")"
        )
        await connection.execute(
            "INSERT INTO memory.pages (id, collection, kind, slug, title, status, card_text, body_hash, tasks)"
            " VALUES ('paper.other', 'research', 'paper', 'other', 'Unrelated widget-frobnicator paper',"
            " 'synthesized', 'paper: Unrelated widget-frobnicator paper', 'hp2', ARRAY['generation'])"
        )
        await connection.execute(
            "INSERT INTO memory.claims (id, page_id, block_id, text, kind, on_dataset, on_task, metric)"
            " VALUES ('paper.graph-cost#c1', 'paper.graph-cost', 'c1', 'Graph indexing costs many tokens.',"
            " 'observation', 'ds.hotpotqa', 'tk.multihopqa', 'mt.f1')"
        )
        await connection.execute("INSERT INTO memory.vocab (vocab, value, definition) VALUES ('facet:tasks', 'retrieval', 'Retrieval tasks')")
    finally:
        await connection.close()


@pytest.fixture
async def matured(pre_maturation: str) -> str:
    """Seed through the old shape, then re-apply 0024 — the fold under test."""
    await _seed_old_shape(pre_maturation)
    _run_alembic(pre_maturation, "0024", downgrade=False)
    return pre_maturation


class TestFold:
    """0024's `UPDATE`/`INSERT` folds land the seeded pre-migration rows in their jsonb homes."""

    async def test_pages_scalars_fold_into_frontmatter(self, matured: str) -> None:
        connection = await _conn(matured)
        try:
            frontmatter = await connection.fetchval("SELECT frontmatter FROM memory.pages WHERE id = 'paper.graph-cost'")
        finally:
            await connection.close()

        assert frontmatter is not None
        assert frontmatter["paper_type"] == "method"
        assert frontmatter["year"] == 2025
        assert frontmatter["venue"] == "EMNLP 2025"
        assert frontmatter["citekey"] == "guo2025graph"
        assert "entity_type" not in frontmatter  # NULL scalars are stripped, not written as JSON null
        assert "maturity" not in frontmatter

    async def test_pages_arrays_fold_into_facets(self, matured: str) -> None:
        connection = await _conn(matured)
        try:
            facets = await connection.fetchval("SELECT facets FROM memory.pages WHERE id = 'paper.graph-cost'")
        finally:
            await connection.close()

        assert facets == {"tasks": ["retrieval"], "domains": ["nlp"], "stages": ["indexing"]}

    async def test_pages_source_id_is_the_old_zotero_key(self, matured: str) -> None:
        connection = await _conn(matured)
        try:
            source_id = await connection.fetchval("SELECT source_id FROM memory.pages WHERE id = 'paper.graph-cost'")
        finally:
            await connection.close()

        assert source_id == "GRAPHKEY1"

    async def test_sources_raw_becomes_vendor_neutral(self, matured: str) -> None:
        connection = await _conn(matured)
        try:
            row = await connection.fetchrow("SELECT source_id, provider, provider_key, meta FROM memory.sources_raw WHERE source_id = 'GRAPHKEY1'")
        finally:
            await connection.close()

        assert row is not None
        assert row["provider"] == "zotero"
        assert row["provider_key"] == "GRAPHKEY1"
        assert row["meta"]["doi"] == "10.18653/v1/2025.x"
        assert row["meta"]["arxiv_id"] == "2410.05779"
        assert row["meta"]["venue"] == "EMNLP 2025"
        assert row["meta"]["year"] == 2025

    async def test_claim_qualifiers_populated_one_row_per_target(self, matured: str) -> None:
        connection = await _conn(matured)
        try:
            rows = await connection.fetch(
                "SELECT qualifier, ord, collection, target_collection, target_id FROM memory.claim_qualifiers"
                " WHERE claim_id = 'paper.graph-cost#c1' ORDER BY qualifier"
            )
        finally:
            await connection.close()

        by_qualifier = {row["qualifier"]: row for row in rows}
        assert set(by_qualifier) == {"on_dataset", "on_task", "metric"}
        assert by_qualifier["on_dataset"]["target_id"] == "ds.hotpotqa"
        assert by_qualifier["on_task"]["target_id"] == "tk.multihopqa"
        assert by_qualifier["metric"]["target_id"] == "mt.f1"
        for row in rows:
            assert row["ord"] == 0
            assert row["collection"] == "research"
            assert row["target_collection"] == "research"


class TestApiSurfaceOverTheMaturedData:
    """`api.search`'s `facets` jsonb parameter and `api.lookup_value` read the new shape."""

    async def test_search_facets_parameter_narrows_to_the_matching_page(self, matured: str) -> None:
        connection = await _conn(matured)
        try:
            narrowed = await connection.fetch(
                "SELECT page_id FROM api.search('widget-frobnicator', k => 10, facets => '{\"tasks\": [\"retrieval\"]}'::jsonb)"
            )
            unnarrowed = await connection.fetch("SELECT page_id FROM api.search('widget-frobnicator', k => 10)")
        finally:
            await connection.close()

        assert {row["page_id"] for row in narrowed} == {"paper.graph-cost"}
        assert {"paper.graph-cost", "paper.other"} <= {row["page_id"] for row in unnarrowed}

    async def test_lookup_value_counts_through_the_facets_map(self, matured: str) -> None:
        connection = await _conn(matured)
        try:
            row = await connection.fetchrow("SELECT value, n_pages FROM api.lookup_value('tasks') WHERE value = 'retrieval'")
        finally:
            await connection.close()

        assert row is not None
        assert row["n_pages"] == 1


class TestDownUpRoundTrip:
    """`downgrade 0023` reconstructs the old columns losslessly; a second `upgrade head` round-trips."""

    async def test_downgrade_reconstructs_the_old_columns(self, matured: str) -> None:
        _run_alembic(matured, "0023", downgrade=True)

        connection = await _conn(matured)
        try:
            page = await connection.fetchrow(
                "SELECT entity_type, paper_type, year, venue, citekey, zotero_key, tasks, domains, stages"
                " FROM memory.pages WHERE id = 'paper.graph-cost'"
            )
            claim = await connection.fetchrow("SELECT on_dataset, on_task, metric FROM memory.claims WHERE id = 'paper.graph-cost#c1'")
            source = await connection.fetchrow("SELECT zotero_key, doi, arxiv_id, venue, year FROM memory.sources_raw WHERE zotero_key = 'GRAPHKEY1'")
        finally:
            await connection.close()

        assert page is not None
        assert page["entity_type"] is None
        assert page["paper_type"] == "method"
        assert page["year"] == 2025
        assert page["venue"] == "EMNLP 2025"
        assert page["citekey"] == "guo2025graph"
        assert page["zotero_key"] == "GRAPHKEY1"
        assert list(page["tasks"]) == ["retrieval"]
        assert list(page["domains"]) == ["nlp"]
        assert list(page["stages"]) == ["indexing"]

        assert claim is not None
        assert claim["on_dataset"] == "ds.hotpotqa"
        assert claim["on_task"] == "tk.multihopqa"
        assert claim["metric"] == "mt.f1"

        assert source is not None
        assert source["doi"] == "10.18653/v1/2025.x"
        assert source["arxiv_id"] == "2410.05779"
        assert source["venue"] == "EMNLP 2025"
        assert source["year"] == 2025

    async def test_reupgrade_after_downgrade_lands_at_head_with_the_fold_intact(self, matured: str) -> None:
        _run_alembic(matured, "0023", downgrade=True)
        _run_alembic(matured, "head", downgrade=False)

        connection = await _conn(matured)
        try:
            version = await connection.fetchval("SELECT version_num FROM alembic_version")
            facets = await connection.fetchval("SELECT facets FROM kb.pages WHERE id = 'paper.graph-cost'")
            qualifier_count = await connection.fetchval("SELECT count(*) FROM kb.claim_qualifiers WHERE claim_id = 'paper.graph-cost#c1'")
        finally:
            await connection.close()

        assert version == _chain_head(matured)
        assert facets == {"tasks": ["retrieval"], "domains": ["nlp"], "stages": ["indexing"]}
        assert qualifier_count == 3
