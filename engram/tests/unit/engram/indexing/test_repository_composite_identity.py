"""Unit specs for the serving-aggregate repositories' composite `(collection, …)` upsert/prune SQL.

Pure, no database: pins the `(collection, …)` conflict targets and scoping predicates on
`engram.indexing.infrastructure.repository`'s "serving" aggregates (pages/sections/claims/raw_chunks/
vocab/edges). The index-state repositories are already covered by
`test_compile_state_repositories.py`. Mirrors that file's `_compiles` pattern; the round-trip
behaviour these imply is `tests/integration/test_repositories.py`'s concern, which stays red
against a live schema still on migration `0024`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy.dialects import postgresql

from engram.indexing.infrastructure.repository import Claims, Edges, Pages, Sections, VocabEntries

if TYPE_CHECKING:
    from sqlalchemy import Delete, Insert, Update
    from sqlalchemy.ext.asyncio import AsyncSession

# `_prune_scoped_statement` is a pure statement factory (touches only `self._model`, never
# `self._db`), so a repo built for this test never opens a session — `cast` narrows the `None`
# placeholder to the declared `AsyncSession` parameter type rather than widening it to `Any`.
_NO_SESSION = cast("AsyncSession", None)


def _compiles(statement: Insert | Delete | Update) -> str:
    """Compile a Core statement against the PostgreSQL dialect; return the SQL text."""
    return str(statement.compile(dialect=postgresql.dialect()))


class TestIdRepositoryUpsertConflictTargets:
    """`pages` / `sections` / `claims`: the upsert conflict target is `(collection, id)`."""

    def test_page_upsert_conflict_targets_collection_id(self):
        sql = _compiles(Pages.upsert_statement([{"id": "p", "collection": "research", "body_hash": "h"}]))

        assert "INSERT INTO kb.pages" in sql
        assert "ON CONFLICT (collection, id)" in sql
        assert "DO UPDATE" in sql

    def test_section_upsert_conflict_targets_collection_id(self):
        sql = _compiles(Sections.upsert_statement([{"id": "p#s", "collection": "research", "body_hash": "h"}]))

        assert "INSERT INTO kb.sections" in sql
        assert "ON CONFLICT (collection, id)" in sql

    def test_claim_upsert_conflict_targets_collection_id(self):
        sql = _compiles(Claims.upsert_statement([{"id": "p#c1", "collection": "research", "text": "t"}]))

        assert "INSERT INTO kb.claims" in sql
        assert "ON CONFLICT (collection, id)" in sql


class TestIdRepositoryPruneScopesDirectlyOnCollection:
    """`_IdRepository._prune_scoped_statement` filters its own `collection` column, no subquery."""

    def test_section_prune_filters_collection_directly_no_pages_subquery(self):
        sql = _compiles(Sections(_NO_SESSION)._prune_scoped_statement({"p#s"}, "research"))

        assert "DELETE FROM kb.sections" in sql
        assert "kb.sections.collection" in sql
        assert "kb.pages" not in sql

    def test_claim_prune_filters_collection_directly_no_pages_subquery(self):
        sql = _compiles(Claims(_NO_SESSION)._prune_scoped_statement({"p#c1"}, "research"))

        assert "DELETE FROM kb.claims" in sql
        assert "kb.claims.collection" in sql
        assert "kb.pages" not in sql


class TestVocabEntriesUpsertConflictTarget:
    """`kb.vocab`: the upsert conflict target is `(collection, vocab, value)`."""

    def test_upsert_conflict_targets_the_three_column_key(self):
        sql = _compiles(VocabEntries.upsert_statement([{"collection": "research", "vocab": "page_type", "value": "x", "definition": "d"}]))

        assert "INSERT INTO kb.vocab" in sql
        assert "ON CONFLICT (collection, vocab, value)" in sql


class TestEdgesPruneScopesDirectlyOnSrcCollection:
    """The whole-collection edge prune reads `src_collection` directly — no `split_part`, no `kb.pages` subquery."""

    def test_delete_collection_statement_has_no_split_part(self):
        sql = _compiles(Edges.delete_collection_statement("research"))

        assert "DELETE FROM kb.edges" in sql
        assert "src_collection" in sql
        assert "split_part" not in sql
        assert "kb.pages" not in sql

    def test_delete_for_sources_statement_scopes_collection_via_src_collection_not_a_pages_subquery(self):
        sql = _compiles(Edges.delete_for_sources_statement({"paper.a"}, "research"))

        assert "DELETE FROM kb.edges" in sql
        assert "src_collection" in sql
        assert "kb.pages" not in sql
        # The owning-page id extraction (a different concern from collection scoping) still needs it.
        assert "split_part" in sql


class TestPageDeletesRemoveTheirClaimsFirst:
    """The claims of the pages a prune or a delete removes are deleted by their own `page_id`, scoped to the collection."""

    def test_the_prune_s_claims_delete_keeps_the_kept_pages_claims(self):
        sql = _compiles(Pages.prune_claims_statement({"note.kept"}, "research"))

        assert "DELETE FROM kb.claims" in sql
        assert "kb.claims.collection" in sql
        assert "NOT (EXISTS" in sql
        assert "kb.claims.page_id = listed.value" in sql

    def test_the_targeted_delete_s_claims_delete_names_the_pages(self):
        sql = _compiles(Pages.delete_claims_by_ids_statement({"note.gone"}, "research"))

        assert "DELETE FROM kb.claims" in sql
        assert "kb.claims.collection" in sql
        assert "kb.claims.page_id IN" in sql


class TestPageDeletesUnsetTheSupersededByNamingThem:
    """A prune or a delete first unsets every `superseded_by` naming a page it removes, scoped to the collection."""

    def test_the_prune_unsets_superseded_by_outside_the_kept_pages(self):
        sql = _compiles(Pages.clear_superseded_by_statement({"note.kept"}, "research"))

        assert "UPDATE kb.pages SET superseded_by" in sql
        assert "kb.pages.collection" in sql
        assert "kb.pages.superseded_by IS NOT NULL" in sql
        assert "NOT (EXISTS" in sql
        assert "kb.pages.superseded_by = listed.value" in sql

    def test_the_targeted_delete_unsets_superseded_by_naming_the_pages(self):
        sql = _compiles(Pages.clear_superseded_by_ids_statement({"note.gone"}, "research"))

        assert "UPDATE kb.pages SET superseded_by" in sql
        assert "kb.pages.collection" in sql
        assert "kb.pages.superseded_by IN" in sql
