"""Unit specs for the four index-state incremental-ingestion repositories.

Pure, no database: each repository's mutating/reading SQL is built by a
`@staticmethod *_statement` factory, indexed against the PostgreSQL dialect, and
asserted on its SQL text (the target table, the conflict target, the WHERE clause).
Mirrors the `TestStatementsCompile` tier in `tests/integration/test_repositories.py`.
The round-trip behaviour these imply is the repositories' integration concern.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.dialects import postgresql

from engram.indexing.infrastructure.repository import (
    ClaimQualifiers,
    Claims,
    IndexFingerprints,
    LinkRefs,
    ManifestEntries,
    Pages,
    PageTypeContracts,
    Sections,
    SourceRepository,
    ValidationIssues,
    _embed_write_rows,
)

if TYPE_CHECKING:
    from sqlalchemy import CompoundSelect, Delete, Insert, Select, Update


def _compiles(statement: Insert | Delete | Select | CompoundSelect | Update) -> str:
    """Index a Core statement against the PostgreSQL dialect; return the SQL text.

    Raises whatever SQLAlchemy raises if the statement is malformed — a green call
    means the repository emitted syntactically valid PostgreSQL (no live server needed).
    """
    return str(statement.compile(dialect=postgresql.dialect()))


class TestManifestEntries:
    """`kb.index_manifest`: upsert on the `(collection, path)` key, delete by path."""

    def test_upsert_conflict_targets_collection_path(self):
        sql = _compiles(
            ManifestEntries.upsert_statement(
                [{"collection": "research", "path": "Memory/a.md", "kind": "note", "content_hash": "c", "identity_hash": "i"}]
            )
        )

        assert "INSERT INTO kb.index_manifest" in sql
        assert "ON CONFLICT" in sql
        assert "DO UPDATE" in sql
        for key in ("collection", "path"):
            assert key in sql

    def test_load_selects_the_collection(self):
        sql = _compiles(ManifestEntries.load_statement("research"))

        assert "FROM kb.index_manifest" in sql
        assert "collection" in sql

    def test_delete_paths_scopes_collection_and_path(self):
        sql = _compiles(ManifestEntries.delete_paths_statement({"Memory/a.md", "Memory/b.md"}, "research"))

        assert "DELETE FROM kb.index_manifest" in sql
        assert "collection" in sql
        assert "path IN" in sql


class TestLinkRefs:
    """`kb.link_refs`: the name/id closure selects, per-source replace, ingest queue."""

    def test_src_pages_referencing_filters_collection_and_raw_target(self):
        sql = _compiles(LinkRefs.src_pages_referencing_statement({"alpha", "beta"}, "research"))

        assert "FROM kb.link_refs" in sql
        assert "src_page_id" in sql
        assert "raw_target IN" in sql
        assert "collection" in sql

    def test_src_pages_pointing_at_filters_collection_and_dst_id(self):
        sql = _compiles(LinkRefs.src_pages_pointing_at_statement({"paper.a", "paper.b"}, "research"))

        assert "FROM kb.link_refs" in sql
        assert "src_page_id" in sql
        assert "dst_id IN" in sql
        assert "collection" in sql

    def test_src_pages_pointing_at_also_reads_the_claims_that_cite_the_pages(self):
        sql = _compiles(LinkRefs.src_pages_pointing_at_statement({"note.a"}, "research"))

        assert "UNION" in sql
        assert "kb.claims.subject_id IN" in sql
        assert "kb.claim_qualifiers.target_id IN" in sql

    def test_delete_for_sources_scopes_collection_and_src_page_id(self):
        sql = _compiles(LinkRefs.delete_for_sources_statement({"paper.a"}, "research"))

        assert "DELETE FROM kb.link_refs" in sql
        assert "collection" in sql
        assert "src_page_id IN" in sql

    def test_insert_targets_link_refs(self):
        sql = _compiles(
            LinkRefs.insert_statement(
                [{"collection": "research", "src_page_id": "paper.a", "src_kind": "page", "rel": "builds_on", "raw_target": "x"}]
            )
        )

        assert "INSERT INTO kb.link_refs" in sql

class TestClaimQualifiers:
    """`kb.claim_qualifiers`: per-page replace (delete by owning claims, insert)."""

    def test_delete_for_pages_scopes_collection_and_owning_claims(self):
        sql = _compiles(ClaimQualifiers.delete_for_pages_statement({"paper.a", "paper.b"}, "research"))

        assert "DELETE FROM kb.claim_qualifiers" in sql
        assert "collection" in sql
        assert "claim_id IN" in sql
        assert "page_id IN" in sql

    def test_insert_targets_claim_qualifiers(self):
        sql = _compiles(
            ClaimQualifiers.insert_statement(
                [
                    {
                        "collection": "research",
                        "claim_id": "paper.a#c1",
                        "qualifier": "on_dataset",
                        "ord": 0,
                        "target_collection": "research",
                        "target_id": "ent.squad",
                    }
                ]
            )
        )

        assert "INSERT INTO kb.claim_qualifiers" in sql


class TestSourceRepositoryUpsertConflictKey:
    """`kb.sources_raw`: the upsert conflict target is `(collection, source_id)` (engram's own vendor-neutral identity, paired)."""

    def test_upsert_conflict_targets_collection_source_id(self):
        sql = _compiles(
            SourceRepository.upsert_sources_statement(
                [
                    {
                        "source_id": "K1",
                        "provider": "zotero",
                        "provider_key": "K1",
                        "collection": "research",
                        "title": "Paper",
                        "source_hash": "sh1",
                    }
                ]
            )
        )

        assert "INSERT INTO kb.sources_raw" in sql
        assert "ON CONFLICT (collection, source_id)" in sql
        assert "DO UPDATE" in sql


class TestValidationIssues:
    """`kb.validation_store`: per-page replace (delete by page_id IN, insert), whole-report fetch."""

    def test_delete_for_pages_scopes_collection_and_page_id(self):
        sql = _compiles(ValidationIssues.delete_for_pages_statement({"paper.a", "paper.b"}, "research"))

        assert "DELETE FROM kb.validation_store" in sql
        assert "collection" in sql
        assert "page_id IN" in sql

    def test_delete_for_collection_clears_every_row_of_that_collection_only(self):
        sql = _compiles(ValidationIssues.delete_for_collection_statement("research"))

        assert "DELETE FROM kb.validation_store" in sql
        assert "collection" in sql
        assert "page_id" not in sql

    def test_insert_targets_validation_store(self):
        sql = _compiles(
            ValidationIssues.insert_statement(
                [{"collection": "research", "page_id": "paper.a", "severity": "error", "rule": "missing-quote", "message": "m"}]
            )
        )

        assert "INSERT INTO kb.validation_store" in sql

    def test_whole_report_selects_the_collection(self):
        sql = _compiles(ValidationIssues.whole_report_statement("research"))

        assert "FROM kb.validation_store" in sql
        assert "collection" in sql


class TestIndexFingerprints:
    """`kb.index_fingerprint`: single-row get + upsert on the `collection` key."""

    def test_get_selects_the_collection(self):
        sql = _compiles(IndexFingerprints.get_statement("research"))

        assert "FROM kb.index_fingerprint" in sql
        assert "collection" in sql

    def test_upsert_conflict_targets_collection(self):
        sql = _compiles(
            IndexFingerprints.upsert_statement(
                {
                    "collection": "research",
                    "indexer_version": "1",
                    "spec_hash": "s",
                    "vocabulary_hash": "r",
                    "embed_model": "m",
                    "knowledge_base_root": "/knowledge_base",
                }
            )
        )

        assert "INSERT INTO kb.index_fingerprint" in sql
        assert "ON CONFLICT" in sql
        assert "DO UPDATE" in sql
        assert "collection" in sql
        assert "knowledge_base_root" in sql

    def test_touch_re_stamps_the_time_and_claims_the_root(self):
        sql = _compiles(
            IndexFingerprints.touch_statement("research", indexed_at=datetime.now(tz=UTC), knowledge_base_root="/knowledge_base")
        )

        assert "UPDATE kb.index_fingerprint" in sql
        assert "indexed_at" in sql
        assert "knowledge_base_root" in sql
        assert "spec" not in sql
        assert "WHERE" in sql

    def test_touch_with_no_root_stamps_the_time_only(self):
        sql = _compiles(
            IndexFingerprints.touch_statement("research", indexed_at=datetime.now(tz=UTC), knowledge_base_root="")
        )

        assert "indexed_at" in sql
        assert "knowledge_base_root" not in sql


class TestPageTypeContracts:
    """`kb.page_type_contracts`: upsert on the `(collection, page_type)` key, prune scoped to `collection`."""

    def test_upsert_conflict_targets_collection_kind(self):
        sql = _compiles(
            PageTypeContracts.upsert_statement(
                [{"collection": "research", "page_type": "flag", "contract": {"page_type": "flag", "status_values": ["pending"]}}]
            )
        )

        assert "INSERT INTO kb.page_type_contracts" in sql
        assert "ON CONFLICT" in sql
        assert "DO UPDATE" in sql
        for key in ("collection", "page_type"):
            assert key in sql

    def test_prune_scopes_collection_and_excludes_the_keep_set(self):
        sql = _compiles(PageTypeContracts.prune_statement({"flag", "paper"}, "research"))

        assert "DELETE FROM kb.page_type_contracts" in sql
        assert "collection" in sql
        assert "NOT (EXISTS" in sql
        assert "kb.page_type_contracts.page_type = listed.value" in sql

    def test_prune_with_an_empty_keep_set_still_scopes_collection(self):
        sql = _compiles(PageTypeContracts.prune_statement(set(), "research"))

        assert "DELETE FROM kb.page_type_contracts" in sql
        assert "collection" in sql
        assert "EXISTS" not in sql


class TestEmbedQueueScoping:
    """The `embedding IS NULL` queues are collection-scoped: one knowledge base's index never claims another's rows.

    Every embedded aggregate carries its own `collection` column (B2), so every queue filters on it
    directly — a `page_id IN (SELECT id FROM kb.pages …)` / `source_id IN (SELECT source_id
    FROM kb.sources_raw …)` subquery could not tell two collections' child rows apart if they
    happened to share a bare owning id.
    """

    def test_pages_queue_filters_on_the_direct_collection_column(self) -> None:
        sql = _compiles(Pages.needing_embedding_statement("research"))

        assert "embedding IS NULL" in sql
        assert "kb.pages.collection =" in sql

    def test_child_queues_filter_on_their_own_direct_collection_column(self) -> None:
        for repository, table in ((Sections, "sections"), (Claims, "claims")):
            sql = _compiles(repository.needing_embedding_statement("research"))

            assert "embedding IS NULL" in sql
            assert "page_id IN" not in sql
            assert f"kb.{table}.collection =" in sql

    def test_chunk_queue_filters_on_its_own_direct_collection_column(self) -> None:
        sql = _compiles(SourceRepository.chunks_needing_embedding_statement("research"))

        assert "embedding IS NULL" in sql
        assert "source_id IN" not in sql
        assert "kb.raw_chunks.collection =" in sql


class TestChunkPruneScoping:
    """`kb.raw_chunks`' prune filters on its own `collection` column, not a `sources_raw` join."""

    def test_prune_chunks_filters_on_the_direct_collection_column(self) -> None:
        sql = _compiles(SourceRepository.prune_chunks_scoped_statement({"K1:0"}, "research"))

        assert "DELETE FROM kb.raw_chunks" in sql
        assert "source_id IN" not in sql
        assert "kb.raw_chunks.collection =" in sql
        assert "NOT (EXISTS" in sql
        assert "kb.raw_chunks.id = listed.value" in sql


class TestEmbedWriteRows:
    """The embed-write bulk-UPDATE param dicts carry `collection` alongside `id` (the composite PK)."""

    def test_embed_write_rows_carry_collection_alongside_id_and_embedding(self) -> None:
        rows = _embed_write_rows({"paper.a": [0.1, 0.2], "paper.b": [0.3, 0.4]}, collection="research")

        assert rows == [
            {"collection": "research", "id": "paper.a", "embedding": [0.1, 0.2]},
            {"collection": "research", "id": "paper.b", "embedding": [0.3, 0.4]},
        ]
