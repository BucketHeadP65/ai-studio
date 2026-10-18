"""`core.db.batches` slicing, the set filters' bind parameter count, and the per-page reconcile DELETEs the incremental load issues.

`batches` keeps a multi-row INSERT under asyncpg's 32767 bind-argument ceiling. Every statement that filters on a
set binds that set as one array parameter, so a set of any size stays under the same ceiling. The
`prune_for_pages` statement factories are indexed against the PostgreSQL dialect with their bind
values rendered inline, so the three predicates every reconcile DELETE must carry (the exact
collection, the owning page set, and absence from the keep set) are read off the SQL text with no
database. The empty-page-set no-op is proven over a session pointed at an unreachable database:
a statement that reached the connection would raise instead of returning 0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core import db
from sqlalchemy import NullPool
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.indexing.infrastructure.repository import (
    ClaimQualifiers,
    Claims,
    Edges,
    LinkRefs,
    ManifestEntries,
    Pages,
    Sections,
    SourceRepository,
    UnitSummaries,
    ValidationIssues,
    VocabEntries,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from sqlalchemy import CompoundSelect, Delete, Select


def _rows(count: int, width: int) -> list[dict[str, object]]:
    return [{f"c{i}": n for i in range(width)} for n in range(count)]


def test_a_small_batch_goes_in_one_statement() -> None:
    rows = _rows(10, 9)

    assert [len(batch) for batch in db.batches(rows)] == [10]


def test_a_full_knowledge_base_of_chunks_is_sliced_under_the_ceiling() -> None:
    """8000 nine-column chunk rows would bind 72000 arguments in one INSERT; they go in three."""
    rows = _rows(8000, 9)

    batches = list(db.batches(rows))

    assert [len(batch) for batch in batches] == [3640, 3640, 720]
    assert all(len(batch) * 9 <= db.MAX_BIND_PARAMETERS for batch in batches)
    assert [row for batch in batches for row in batch] == rows


def test_no_rows_means_no_statement() -> None:
    assert list(db.batches([])) == []


# -- the set filters ------------------------------------------------------------

# More values than one statement may bind.
_MANY = frozenset(f"absent.{n}" for n in range(40_000))
_MANY_PAIRS = frozenset(("absent", f"value.{n}") for n in range(40_000))

_SET_FILTERS: dict[str, Callable[[], Delete | Select | CompoundSelect]] = {
    "id prune": lambda: Sections(AsyncSession())._prune_scoped_statement(set(_MANY), "research"),
    "page prune": lambda: Pages(AsyncSession())._prune_scoped_statement(set(_MANY), "research"),
    "page prune claims": lambda: Pages.prune_claims_statement(set(_MANY), "research"),
    "page delete claims": lambda: Pages.delete_claims_by_ids_statement(set(_MANY), "research"),
    "page delete": lambda: Pages.delete_by_ids_statement(set(_MANY), "research"),
    "page child reconcile": lambda: Claims.prune_for_pages_statement(set(_MANY), set(_MANY), "research"),
    "removed sections": lambda: UnitSummaries.removed_section_ids_statement(set(_MANY), set(_MANY), "research"),
    "summary delete": lambda: UnitSummaries.delete_units_statement(set(_MANY), "research"),
    "summary prune": lambda: UnitSummaries.prune_statement(set(_MANY), "research"),
    "vocabulary prune": lambda: VocabEntries.prune_statement(set(_MANY_PAIRS), "research"),
    "edge delete": lambda: Edges.delete_for_sources_statement(set(_MANY), "research"),
    "source prune": lambda: SourceRepository.prune_sources_scoped_statement(set(_MANY), "research"),
    "source delete": lambda: SourceRepository.delete_sources_by_keys_statement(set(_MANY), "research"),
    "chunk prune": lambda: SourceRepository.prune_chunks_scoped_statement(set(_MANY), "research"),
    "manifest delete": lambda: ManifestEntries.delete_paths_statement(set(_MANY), "research"),
    "pages linking at names": lambda: LinkRefs.src_pages_referencing_statement(set(_MANY), "research"),
    "pages pointing at ids": lambda: LinkRefs.src_pages_pointing_at_statement(set(_MANY), "research"),
    "link ref delete": lambda: LinkRefs.delete_for_sources_statement(set(_MANY), "research"),
    "claim qualifier delete": lambda: ClaimQualifiers.delete_for_pages_statement(set(_MANY), "research"),
    "validation delete": lambda: ValidationIssues.delete_for_pages_statement(set(_MANY), "research"),
}


def _bound_parameters(statement: Delete | Select | CompoundSelect) -> int:
    """How many parameters `statement` binds when asyncpg runs it, every expanding `IN` list spelled out."""
    compiled = statement.compile(dialect=asyncpg.dialect(), compile_kwargs={"render_postcompile": True})
    return len(compiled.positiontup or ())


@pytest.mark.parametrize("build", list(_SET_FILTERS.values()), ids=list(_SET_FILTERS))
def test_a_set_filter_binds_a_few_parameters_however_large_the_set(build: Callable[[], Delete | Select | CompoundSelect]) -> None:
    assert _bound_parameters(build()) <= 8


# -- the per-page reconcile statements ---------------------------------------

# A database nothing listens on: reaching it is the failure signal an empty-input no-op must avoid.
_UNREACHABLE_DSN = "postgresql+asyncpg://engram:engram@127.0.0.1:1/unreachable"


def _literal(statement: Delete | Select) -> str:
    """Index a Core statement against the PostgreSQL dialect with its bind values rendered inline, on one line."""
    return " ".join(str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})).split())


@pytest.fixture
async def offline_session() -> AsyncIterator[AsyncSession]:
    """A session over an unreachable database, so any statement issued raises instead of returning."""
    engine = create_async_engine(_UNREACHABLE_DSN, poolclass=NullPool)
    session = AsyncSession(engine)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


class TestPageChildReconcile:
    """`kb.sections` / `kb.claims`: delete the named pages' children that the fresh load did not produce."""

    def test_the_sections_delete_names_the_collection_the_owning_pages_and_the_keep_set(self) -> None:
        sql = _literal(Sections.prune_for_pages_statement({"paper.x#tl_dr"}, {"paper.x"}, "research"))

        assert "DELETE FROM kb.sections" in sql
        assert "kb.sections.collection = 'research'" in sql
        assert "kb.sections.page_id IN (SELECT unnest(ARRAY['paper.x'])" in sql
        assert "NOT (EXISTS (SELECT * FROM unnest(ARRAY['paper.x#tl_dr']) AS listed(value) WHERE kb.sections.id = listed.value))" in sql

    def test_the_claims_delete_names_the_collection_the_owning_pages_and_the_keep_set(self) -> None:
        sql = _literal(Claims.prune_for_pages_statement({"paper.x#c1"}, {"paper.x"}, "research"))

        assert "DELETE FROM kb.claims" in sql
        assert "kb.claims.collection = 'research'" in sql
        assert "kb.claims.page_id IN (SELECT unnest(ARRAY['paper.x'])" in sql
        assert "NOT (EXISTS (SELECT * FROM unnest(ARRAY['paper.x#c1']) AS listed(value) WHERE kb.claims.id = listed.value))" in sql

    def test_an_empty_keep_set_deletes_every_child_of_the_named_pages(self) -> None:
        """A page that now produces no section at all: every section row it owns is stale."""
        sql = _literal(Sections.prune_for_pages_statement(set(), {"paper.x"}, "research"))

        assert "kb.sections.page_id IN (SELECT unnest(ARRAY['paper.x'])" in sql
        assert "NOT (EXISTS" not in sql

    def test_only_the_named_collections_rows_are_eligible(self) -> None:
        """`(research, paper.x#old)` and `(repo-docs, paper.x#old)` share a page and a section id.

        The collection equality is the only predicate that separates them, and it names `research`,
        so the second knowledge base's identically-keyed row can never be reached.
        """
        sql = _literal(Sections.prune_for_pages_statement({"paper.x#new"}, {"paper.x"}, "research"))

        assert "kb.sections.collection = 'research'" in sql
        assert "repo-docs" not in sql

    async def test_an_empty_page_set_issues_no_delete(self, offline_session: AsyncSession) -> None:
        repo = Sections(offline_session)

        assert await repo.prune_for_pages({"paper.x#tl_dr"}, set(), collection="research") == 0


class TestUnitSummaryReconcile:
    """`kb.unit_summaries`: a summary outlives its unit unless the reconcile names the gone unit ids."""

    def test_the_removed_section_select_names_the_collection_the_owning_pages_and_the_keep_set(self) -> None:
        sql = _literal(UnitSummaries.removed_section_ids_statement({"paper.x#tl_dr"}, {"paper.x"}, "research"))

        assert "FROM kb.sections" in sql
        assert "kb.sections.collection = 'research'" in sql
        assert "kb.sections.page_id IN (SELECT unnest(ARRAY['paper.x'])" in sql
        assert "NOT (EXISTS (SELECT * FROM unnest(ARRAY['paper.x#tl_dr']) AS listed(value) WHERE kb.sections.id = listed.value))" in sql

    def test_the_summary_delete_names_the_collection_and_the_gone_unit_ids(self) -> None:
        sql = _literal(UnitSummaries.delete_units_statement({"paper.x#old"}, "research"))

        assert "DELETE FROM kb.unit_summaries" in sql
        assert "kb.unit_summaries.collection = 'research'" in sql
        assert "kb.unit_summaries.unit_id IN (SELECT unnest(ARRAY['paper.x#old'])" in sql

    def test_the_whole_collection_prune_keeps_only_the_current_units(self) -> None:
        sql = _literal(UnitSummaries.prune_statement({"paper.x", "paper.x#tl_dr"}, "research"))

        assert "DELETE FROM kb.unit_summaries" in sql
        assert "kb.unit_summaries.collection = 'research'" in sql
        assert (
            "NOT (EXISTS (SELECT * FROM unnest(ARRAY['paper.x', 'paper.x#tl_dr']) AS listed(value) WHERE kb.unit_summaries.unit_id = listed.value))"
            in sql
        )

    def test_an_empty_keep_set_deletes_every_summary_of_the_collection(self) -> None:
        """A knowledge base that indexes no page or section keeps no summary either."""
        sql = _literal(UnitSummaries.prune_statement(set(), "research"))

        assert "kb.unit_summaries.collection = 'research'" in sql
        assert "NOT (EXISTS" not in sql

    async def test_no_owning_pages_issues_no_select_or_delete(self, offline_session: AsyncSession) -> None:
        repo = UnitSummaries(offline_session)

        assert await repo.prune_for_pages({"paper.x#tl_dr"}, set(), set(), collection="research") == 0
