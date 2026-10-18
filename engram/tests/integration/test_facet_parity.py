r"""`api.search`'s unified `facets` jsonb parameter narrows every declared facet — slotted or not.

Before the spine-maturation fold, `api.search` narrowed only through three hardcoded parameters
(`task`/`stage`/`domain`); a facet a knowledge base declared outside that trio lost search narrowing
entirely (`FacetSpec.slot=None`). This module proves the unified `facets` parameter now narrows
the SAME rows the old typed parameters would have for the research knowledge base's own task/domain/stage
facets, and ALSO narrows a facet a non-research knowledge base declares that never had a slot at all —
proving the narrowing is generic to whatever a `KnowledgeBaseSchema` declares, not hardcoded to five names.

Every narrowing test seeds one page whose facets carry the value under test plus a same-collection
distractor page carrying a DIFFERENT value for the same facet key, so a query that returns the
distractor (or both) fails just as loudly as one that returns neither.

Runs against the `migrated` fixture (head, `KNOWLEDGE_BASE_TEST_DIALECT=paradedb` required on this branch:
the AGE default bed cannot build a partitioned `api.graph_expand`/`api.graph_paths` past 0025 and
refuses the whole migration by design). Skips the whole tier when Docker is unavailable.
"""

from __future__ import annotations

import json

import asyncpg

from tests.integration.test_migrations import _asyncpg_dsn


async def _conn(url: str) -> asyncpg.Connection:
    """A raw asyncpg connection with the jsonb codec registered (so `jsonb` columns decode to dicts)."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    await connection.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    return connection


async def _insert_page(
    connection: asyncpg.Connection, *, page_id: str, collection: str, title: str, card_text: str, facets: dict[str, list[str]]
) -> None:
    """Insert one minimal `kb.pages` row carrying `facets`, distinguishable by `card_text`'s shared search term."""
    await connection.execute(
        "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash, facets)"
        " VALUES ($1, $2, 'paper', $3, $4, 'verified', $5, $6, $7::jsonb)",
        page_id,
        collection,
        page_id.split(".", 1)[-1],
        title,
        card_text,
        f"h-{page_id}",
        facets,
    )


# A term found in no real indexed knowledge base, shared by every research-collection fixture page below so
# `api.search`'s BM25 leg matches all four and the `facets` parameter alone does the narrowing.
_RESEARCH_TERM = "parityfrobnicator"

# Same idea, for the non-research ("cookbook") collection's two fixture pages.
_COOKBOOK_TERM = "parityfrobnicookbook"


async def _seed_research_facets(connection: asyncpg.Connection) -> None:
    """Seed four `research`-collection pages: one for each of tasks/domains/stages, plus a distractor.

    The distractor carries a DIFFERENT value on all three facets, so a narrowing that (wrongly)
    ignored the `facets` predicate would surface it alongside the intended page.
    """
    await _insert_page(
        connection,
        page_id="paper.parity-task",
        collection="research",
        title="Parity Task Page",
        card_text=f"{_RESEARCH_TERM} paper narrowed by its task facet",
        facets={"tasks": ["parity-task"]},
    )
    await _insert_page(
        connection,
        page_id="paper.parity-domain",
        collection="research",
        title="Parity Domain Page",
        card_text=f"{_RESEARCH_TERM} paper narrowed by its domain facet",
        facets={"domains": ["parity-domain"]},
    )
    await _insert_page(
        connection,
        page_id="paper.parity-stage",
        collection="research",
        title="Parity Stage Page",
        card_text=f"{_RESEARCH_TERM} paper narrowed by its stage facet",
        facets={"stages": ["parity-stage"]},
    )
    await _insert_page(
        connection,
        page_id="paper.parity-distractor",
        collection="research",
        title="Parity Distractor Page",
        card_text=f"{_RESEARCH_TERM} paper that must NEVER match any of the three narrowings above",
        facets={"tasks": ["other-task"], "domains": ["other-domain"], "stages": ["other-stage"]},
    )


async def _seed_cookbook_facets(connection: asyncpg.Connection) -> None:
    """Seed two `cookbook`-collection pages (a non-research knowledge base) narrowable by a facet with no prior slot.

    `cuisine` names no `FacetSlot` `api.search` ever hardcoded — this proves a facet ANY knowledge base
    declares, not only the five research-specific names, narrows through the same `facets` parameter.
    """
    await _insert_page(
        connection,
        page_id="rec.parity-italian",
        collection="cookbook",
        title="Parity Italian Recipe",
        card_text=f"{_COOKBOOK_TERM} recipe narrowed by its cuisine facet",
        facets={"cuisine": ["italian"]},
    )
    await _insert_page(
        connection,
        page_id="rec.parity-french",
        collection="cookbook",
        title="Parity French Recipe",
        card_text=f"{_COOKBOOK_TERM} recipe that must NEVER match the italian narrowing below",
        facets={"cuisine": ["french"]},
    )


class TestResearchFacetParity:
    """The research knowledge base's `tasks`/`domains`/`stages` narrow through `facets`, each excluding the distractor."""

    async def test_tasks_facet_narrows_to_the_matching_page_only(self, migrated: str) -> None:
        connection = await _conn(migrated)
        try:
            await _seed_research_facets(connection)
            rows = await connection.fetch(
                "SELECT page_id FROM api.search($1, k => 10, facets => $2::jsonb)",
                _RESEARCH_TERM,
                {"tasks": ["parity-task"]},
            )
        finally:
            await connection.close()

        assert {row["page_id"] for row in rows} == {"paper.parity-task"}

    async def test_domains_facet_narrows_to_the_matching_page_only(self, migrated: str) -> None:
        connection = await _conn(migrated)
        try:
            await _seed_research_facets(connection)
            rows = await connection.fetch(
                "SELECT page_id FROM api.search($1, k => 10, facets => $2::jsonb)",
                _RESEARCH_TERM,
                {"domains": ["parity-domain"]},
            )
        finally:
            await connection.close()

        assert {row["page_id"] for row in rows} == {"paper.parity-domain"}

    async def test_stages_facet_narrows_to_the_matching_page_only(self, migrated: str) -> None:
        connection = await _conn(migrated)
        try:
            await _seed_research_facets(connection)
            rows = await connection.fetch(
                "SELECT page_id FROM api.search($1, k => 10, facets => $2::jsonb)",
                _RESEARCH_TERM,
                {"stages": ["parity-stage"]},
            )
        finally:
            await connection.close()

        assert {row["page_id"] for row in rows} == {"paper.parity-stage"}

    async def test_unnarrowed_search_still_surfaces_all_four_pages(self, migrated: str) -> None:
        """Pre-condition sanity: without a `facets` narrowing, the BM25 leg alone surfaces all four seeded pages."""
        connection = await _conn(migrated)
        try:
            await _seed_research_facets(connection)
            rows = await connection.fetch("SELECT page_id FROM api.search($1, k => 10)", _RESEARCH_TERM)
        finally:
            await connection.close()

        assert {row["page_id"] for row in rows} == {
            "paper.parity-task",
            "paper.parity-domain",
            "paper.parity-stage",
            "paper.parity-distractor",
        }


class TestGenericKnowledgeBaseFacetParity:
    """A facet a non-research knowledge base declares (no `FacetSlot` ever named it) narrows through `facets` too."""

    async def test_a_never_slotted_facet_narrows_to_the_matching_page_only(self, migrated: str) -> None:
        connection = await _conn(migrated)
        try:
            await _seed_cookbook_facets(connection)
            rows = await connection.fetch(
                "SELECT page_id FROM api.search($1, k => 10, facets => $2::jsonb, collection => 'cookbook')",
                _COOKBOOK_TERM,
                {"cuisine": ["italian"]},
            )
        finally:
            await connection.close()

        assert {row["page_id"] for row in rows} == {"rec.parity-italian"}
