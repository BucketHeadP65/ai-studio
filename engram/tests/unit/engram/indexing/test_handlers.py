r"""Unit tests for the indexing handler (`src/engram/indexing/service/index.py`).

The handler is the five-stage rebuild orchestrator. These
specs drive it entirely through fakes — :class:`FakeKnowledgeBaseUnitOfWork` (recording repos),
:class:`FakeEmbedding`, :class:`FakeValidationReportSink`, a :class:`BusSpy`, and a
`knowledge_base_builder` tmp knowledge base — so there is no database, no network, and no real
filesystem write-back. They pin:

* the 5-stage happy path: every aggregate is upserted and the report counts the rows;
* hash-gated re-embed *at the handler level*: run 1 embeds the queued units, run 2 (an
  unchanged knowledge base → empty needing-set) embeds 0 and reuses all;
* `--no-embed`: the Embed stage is skipped (no embedder call, no EmbeddingsRefreshed);
* prune-missing is called per aggregate with the current id set (full rebuild);
* a parse-failure note becomes a validation ERROR and the run continues;
* validation errors do NOT abort the Load (flagged-but-loaded contract);
* the event sequence the bus drains: IndexingStarted → NotesParsed → ValidationCompleted →
  PagesLoaded → CitingPagesChanged → [EmbeddingsRefreshed] → IndexingCompleted;
* the validation report sink receives the validation report.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, cast

import pytest

from core.cqrs.contracts import ImmediateWake, MemoryJobs, MemoryOutbox, MessageBus, Worker

from engram.indexing.contracts import (
    DEFAULT_CHUNK_SIZES,
    CitingPagesChanged,
    CollectionIsForeignError,
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    IndexReport,
    ManifestEntry,
    PagesLoaded,
    indexer_version,
)
from engram.indexing.domain.manifest import FileType, identity_hash
from engram.indexing.domain.models import EmbeddingsRefreshed, IndexingCompleted, IndexingStarted, NotesParsed, ValidationCompleted
from engram.indexing.read_models import CleanPage, PendingEmbedChunk, PendingEmbedRow, StoredFingerprint
from engram.indexing.service.orientation import orientation_page_id, orientation_page_ids
from engram.kb.domain.schema import EmbedStrategiesSpec
from engram.testing.fakes.bus_spy import BusSpy
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork, FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema

# A minimal well-formed paper note (valid frontmatter + one canonical section + one claim).
_PAPER = """\
---
id: paper.lightrag-2025
type: paper
title: LightRAG
summary: Graph-indexed RAG.
status: synthesized
zotero_key: 8QXFXYMI
year: 2025
paper_type: method
source_hash: bd72
tasks: [long-doc-qa]
stages: [retrieval]
proposes: ["[[lightrag]]"]
---

## TL;DR

LightRAG indexes a knowledge base as a graph and retrieves at two levels.

## Findings

- LightRAG wins on the large UltraDomain subsets. ^c1
    - loc:: §4.2, Table 1
    - quote:: "LightRAG consistently outperforms GraphRAG, particularly in larger datasets with complex language contexts"
    - kind:: benchmark-result
    - outperforms:: [[graphrag]]
    - on_dataset:: [[ultradomain]]
    - metric:: [[llm-judged-win-rate]]
"""

# The entity the claim's subject/edges point at — so the link resolves (no dangling warning).
_ENTITY = """\
---
id: ent.lightrag
type: entity
title: LightRAG
status: draft
entity_type: technique
---

## Definition

A graph-indexed retrieval technique.
"""

# A raw Sources/ mirror carrying the quote (so quote-in-mirror verification passes) keyed by `source_key`.
_MIRROR = """\
---
source_key: 8QXFXYMI
provider: example-library
source_hash: bd72
---
# LightRAG: Simple and Fast Retrieval-Augmented Generation

## 3 The LightRAG Architecture

LightRAG consistently outperforms GraphRAG, particularly in larger datasets with complex language contexts.
"""

# A raw Sources/ mirror naming no `provider` — the `mirror-without-provider` validation error's fixture.
# Cited by no page, so it triggers no `dangling-source-ref` warning of its own.
_ORPHAN_MIRROR = """\
---
source_key: ORPHAN1
source_hash: aa11
---
# An Uncredited Mirror

Nobody cites this one; it exists only to trip the missing-provider check.
"""


def _build_knowledge_base(tmp_path: Path) -> Path:
    """A mini knowledge base with one paper, one entity, one matching mirror, vocabularies + meta stubs."""
    builder = KnowledgeBaseBuilder(tmp_path)
    builder.add_unit_document("Wiki/Papers", "lightrag-2025", _PAPER)
    builder.add_unit_document("Wiki/Entities", "lightrag", _ENTITY)
    builder.add_source_mirror("lightrag-2025", _MIRROR)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    return tmp_path


def _handler(
    knowledge_base_root: Path,
    *,
    uow: FakeKnowledgeBaseUnitOfWork,
    embedder: FakeEmbedding | None = None,
    validation_report_sink: FakeValidationReportSink | None = None,
    spec: KnowledgeBaseSchema | None = None,
) -> IndexKnowledgeBaseHandler:
    """Build the handler over a real filesystem reader + fakes; the clock is a deterministic stub."""
    ticks = iter([0.0, 1.5, 3.0, 4.5, 6.0])
    return IndexKnowledgeBaseHandler(
        FilesystemKnowledgeBaseReader(knowledge_base_root, note_folders=(spec or research_spec()).note_folders),
        embedder or FakeEmbedding(),
        lambda: uow,
        validation_report_sink or FakeValidationReportSink(),
        spec=spec or research_spec(),
        clock=lambda: next(ticks),
    )


def _claimed_fingerprint(
    *, knowledge_base_root: str, indexer_version: str = "", spec_hash: str = "", vocabulary_hash: str = "", embed_model: str = ""
) -> StoredFingerprint:
    """A stored `index_fingerprint` row carrying the knowledge base root that indexed it."""
    return StoredFingerprint(
        indexer_version=indexer_version,
        spec_hash=spec_hash,
        vocabulary_hash=vocabulary_hash,
        embed_model=embed_model,
        knowledge_base_root=knowledge_base_root,
    )


class TestForeignCollectionGuard:
    async def test_a_collection_claimed_by_another_root_refuses_before_any_write(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.fingerprint.stored = _claimed_fingerprint(knowledge_base_root="/their/knowledge_base")
        handler = _handler(knowledge_base, uow=uow)

        with pytest.raises(CollectionIsForeignError) as caught:
            await handler.handle(IndexKnowledgeBase(collection="research"))

        assert caught.value.collection == "research"
        assert caught.value.stored_root == "/their/knowledge_base"
        assert caught.value.knowledge_base_root == str(knowledge_base)
        assert uow.fingerprint.fingerprint is None

    async def test_naming_the_collection_takes_it_over_with_a_full_rebuild(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.fingerprint.stored = _claimed_fingerprint(knowledge_base_root="/their/knowledge_base")
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research", collection_named=True))

        assert report.mode == "full"
        assert uow.fingerprint.fingerprint is not None
        assert uow.fingerprint.fingerprint["knowledge_base_root"] == str(knowledge_base)

    async def test_an_unclaimed_collection_is_claimed_by_its_first_compile(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        assert uow.fingerprint.fingerprint is not None
        assert uow.fingerprint.fingerprint["knowledge_base_root"] == str(knowledge_base)

    async def test_a_pre_guard_row_with_an_empty_root_compiles_and_claims(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.fingerprint.stored = _claimed_fingerprint(knowledge_base_root="")
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        assert uow.fingerprint.fingerprint is not None
        assert uow.fingerprint.fingerprint["knowledge_base_root"] == str(knowledge_base)

    async def test_a_dry_run_previews_a_foreign_collection_without_refusing(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.fingerprint.stored = _claimed_fingerprint(knowledge_base_root="/their/knowledge_base")
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research", dry_run=True))

        assert report.dry_run is not None
        assert uow.fingerprint.fingerprint is None

    async def test_a_foreign_root_forces_the_full_path_even_on_a_matching_fingerprint(self, tmp_path: Path):
        # The incremental gate itself: with a matching fingerprint and a non-empty manifest, only
        # the stored knowledge base root separates the two calls, so False pins the root check alone.
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        embedder = FakeEmbedding()
        handler = _handler(knowledge_base, uow=uow, embedder=embedder)
        uow.manifest.stored = {
            "Memory/Papers/lightrag-2025.md": ManifestEntry(
                path="Memory/Papers/lightrag-2025.md",
                kind=FileType.NOTE,
                content_hash="ch",
                identity_hash=identity_hash(stem="lightrag-2025", id_="paper.lightrag-2025", title="LightRAG", aliases=()),
                page_id="paper.lightrag-2025",
                source_key=None,
            )
        }
        matching = {
            "indexer_version": indexer_version(DEFAULT_CHUNK_SIZES),
            "spec_hash": "spec-hash",
            "vocabulary_hash": "vocabulary-hash",
            "embed_model": embedder.model_id,
        }
        gate = handler._can_incremental

        uow.fingerprint.stored = _claimed_fingerprint(knowledge_base_root=str(knowledge_base), **matching)
        assert await gate("research", "spec-hash", "vocabulary-hash") is True

        uow.fingerprint.stored = _claimed_fingerprint(knowledge_base_root="/their/knowledge_base", **matching)
        assert await gate("research", "spec-hash", "vocabulary-hash") is False
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        changed = [event for event in uow.coordination.events if isinstance(event, CitingPagesChanged)]
        assert len(changed) == 1
        assert changed[0].page_ids == ("paper.lightrag-2025",)

    async def test_report_counts_every_loaded_aggregate(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        assert isinstance(report, IndexReport)
        assert report.pages == 3  # paper + entity + the meta.about content page
        assert report.claims == 1
        assert report.sections >= 2  # at least TL;DR + Findings + Definition
        assert uow.committed is True

    async def test_pages_are_upserted_with_card_text(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        page_ids = {row["id"] for row in uow.pages.upserted_rows}
        assert page_ids == {"paper.lightrag-2025", "ent.lightrag", "meta.about@research"}
        paper = next(row for row in uow.pages.upserted_rows if row["id"] == "paper.lightrag-2025")
        assert "LightRAG" in str(paper["card_text"])

    async def test_a_created_date_in_frontmatter_lands_on_the_page_row(self, tmp_path: Path):
        """`pages.created` is what a later pass reads as "written": a bare or quoted ISO date both land."""
        knowledge_base = tmp_path / "knowledge_base"
        builder = KnowledgeBaseBuilder(knowledge_base)
        builder.add_unit_document("Wiki/Papers", "lightrag-2025", _PAPER.replace("type: paper\n", "type: paper\ncreated: '2026-08-26'\n", 1))
        builder.add_unit_document("Wiki/Entities", "lightrag", _ENTITY)
        builder.add_source_mirror("lightrag-2025", _MIRROR)
        builder.add_vocabulary()
        builder.add_meta_stubs()
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        paper = next(row for row in uow.pages.upserted_rows if row["id"] == "paper.lightrag-2025")
        assert paper["created"] == date(2026, 8, 26)

    async def test_sources_and_chunks_loaded_from_mirror(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        source_keys = {row["source_id"] for batch in uow.sources.sources for row in batch}
        assert "8QXFXYMI" in source_keys
        assert len(uow.sources.upserted_chunks) >= 1

    async def test_no_validation_errors_for_well_formed_knowledge_base(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        assert report.validation.n_errors == 0

    async def test_sections_are_upserted_with_a_derived_summary(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        tldr = next(row for row in uow.sections.upserted_rows if row["id"] == "paper.lightrag-2025#tl_dr")
        assert tldr["summary"] == "LightRAG indexes a knowledge base as a graph and retrieves at two levels."


class TestDuplicateId:
    async def test_duplicate_page_id_is_a_clean_lint_error_and_loads_once(self, tmp_path: Path):
        # Two notes resolving to the same page id must NOT reach upsert as two rows (in real
        # Postgres that is a CardinalityViolationError): the collision becomes a duplicate-id
        # validation error and only the first note loads.
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_unit_document("Wiki/Papers", "lightrag-2025", _PAPER)
        builder.add_unit_document("Wiki/Papers", "lightrag-copy", _PAPER)  # same frontmatter id
        builder.add_source_mirror("lightrag-2025", _MIRROR)
        builder.add_vocabulary()
        builder.add_meta_stubs()
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(tmp_path, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        dup_errors = [issue for issue in report.validation.errors if issue.rule == "duplicate-id"]
        assert len(dup_errors) == 1
        assert dup_errors[0].page_id == "paper.lightrag-2025"
        loaded = [row for row in uow.pages.upserted_rows if row["id"] == "paper.lightrag-2025"]
        assert len(loaded) == 1  # deduped — only the first note loaded, no PK collision at upsert


class TestEmbedStage:
    async def test_run_one_embeds_the_queued_units(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        # Script the needing-set: the page + its one claim need embedding on a fresh load.
        uow = FakeKnowledgeBaseUnitOfWork(
            pages_needing=[PendingEmbedRow("paper.lightrag-2025")], claims_needing=[PendingEmbedRow("paper.lightrag-2025#c1")]
        )
        embedder = FakeEmbedding()
        handler = _handler(knowledge_base, uow=uow, embedder=embedder)

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=True))

        assert report.embedded == 2
        assert len(embedder.texts) == 2  # the two units were embedded
        assert report.model_dump()["embedded"] == 2

    async def test_run_one_persists_vectors_through_repo_writes(self, tmp_path: Path):
        # The embed stage must PERSIST vectors via the repos' write_embeddings /
        # write_chunk_embeddings (keyed id -> vector), not mutate detached ORM rows whose
        # session has closed. Each embedded aggregate gets exactly its needing unit's vector.
        knowledge_base = _build_knowledge_base(tmp_path)
        # The queued ids must be REAL loaded rows (their embed inputs exist): a section id that no
        # row produced would embed `""`, which the `_embed_unit` guard refuses.
        uow = FakeKnowledgeBaseUnitOfWork(
            pages_needing=[PendingEmbedRow("paper.lightrag-2025")],
            sections_needing=[PendingEmbedRow("paper.lightrag-2025#tl_dr")],
            claims_needing=[PendingEmbedRow("paper.lightrag-2025#c1")],
            chunks_needing=[PendingEmbedChunk("8QXFXYMI:0", "")],
        )
        embedder = FakeEmbedding(dimensions=1024)
        handler = _handler(knowledge_base, uow=uow, embedder=embedder)

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=True))

        assert report.embedded == 4
        # Every aggregate persisted its one queued id, each a 1024-dim vector, through the repo.
        assert [pair[0] for pair in uow.pages.embedded] == ["paper.lightrag-2025"]
        assert [pair[0] for pair in uow.sections.embedded] == ["paper.lightrag-2025#tl_dr"]
        assert [pair[0] for pair in uow.claims.embedded] == ["paper.lightrag-2025#c1"]
        assert [pair[0] for pair in uow.sources.embedded_chunks] == ["8QXFXYMI:0"]
        assert all(len(vector) == 1024 for _id, vector in uow.pages.embedded)

    async def test_run_two_unchanged_knowledge_base_embeds_zero(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()  # empty needing-set: nothing changed, nothing re-embeds
        embedder = FakeEmbedding()
        handler = _handler(knowledge_base, uow=uow, embedder=embedder)

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=True))

        assert report.embedded == 0
        assert embedder.texts == []
        assert report.reused > 0  # the loaded units were reused, not re-embedded

    async def test_embed_meta_is_written_with_model_and_dimensions(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        embedder = FakeEmbedding(model="fake-embed", dimensions=1024)
        handler = _handler(knowledge_base, uow=uow, embedder=embedder)

        await handler.handle(IndexKnowledgeBase(collection="research", embed=True))

        assert uow.meta.embed_meta == ("fake-embed", 1024)

    async def test_no_embed_flag_skips_the_embed_stage(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow("paper.lightrag-2025")])
        embedder = FakeEmbedding()
        handler = _handler(knowledge_base, uow=uow, embedder=embedder)

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert report.embedded == 0
        assert embedder.texts == []  # the embedder was never called
        assert uow.meta.embed_meta is None  # embed_meta is not touched when embedding is skipped


class TestOrientationAudience:
    """Which orientation surface each `_meta/` document lands on, and who decides.

    `api.about` is the L0 read a QUERY agent makes once per session; `api.authoring_rules` is the
    contract for whoever writes pages. Before the split, one concatenated `meta.about` row carried
    both -- 19,000 of the RAG memory's 26,370 orientation characters were the authoring and ingestion
    contract, delivered to a consumer that never ingests. The knowledge base's own Consumers table already
    named two audiences with two surfaces; the indexer was what ignored it.

    The document declares, the indexer routes, and nothing anywhere parses a heading.
    """

    @staticmethod
    def _orientation(uow: FakeKnowledgeBaseUnitOfWork) -> dict[str, str]:
        """Every synthetic orientation page that was upserted, base id (`meta.about` / `meta.authoring`) -> body.

        Keyed by the unscoped base id, not the real (collection-scoped) row id: this class is about
        which surface a document lands on, not the id-scoping `TestOrientationPageCollectionScoping`
        already covers.
        """
        return {
            str(page["id"]).split("@", 1)[0]: str(page["card_text"])
            for page in uow.pages.upserted_rows
            if str(page["id"]).startswith("meta.")
        }

    async def test_an_undeclared_document_stays_on_the_query_surface(self, tmp_path: Path):
        """The compatibility default, and the reason adopting this needs no migration.

        A knowledge base that has never heard of `audience` indexes to exactly what it indexed to before:
        one `meta.about` row with every orientation document in it, and no second page. So
        `api.about` returns byte-identical bytes and `api.authoring_rules` is honestly empty.
        """
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        await _handler(knowledge_base, uow=uow).handle(IndexKnowledgeBase(collection="research"))

        pages = self._orientation(uow)
        assert set(pages) == {"meta.about"}, "an undeclared knowledge base must not grow a second orientation page"
        assert "The binding note-grammar contract" in pages["meta.about"]
        assert "Query-time doctrine" in pages["meta.about"]

    async def test_a_declared_authoring_document_leaves_the_query_surface(self, tmp_path: Path):
        """One frontmatter line moves 19k out of a query agent's L0 read. That is the whole feature."""
        knowledge_base = _build_knowledge_base(tmp_path)
        schema = knowledge_base / "_meta" / "schema.md"
        schema.write_text("---\naudience: authoring\n---\n\n# Schema\n\nThe note-grammar contract.\n", encoding="utf-8")
        uow = FakeKnowledgeBaseUnitOfWork()
        await _handler(knowledge_base, uow=uow).handle(IndexKnowledgeBase(collection="research"))

        pages = self._orientation(uow)
        assert set(pages) == {"meta.about", "meta.authoring"}
        assert "note-grammar contract" in pages["meta.authoring"], "the authoring contract moved to its own surface"
        assert "note-grammar contract" not in pages["meta.about"], "and left the query agent's L0 read"
        assert "Query-time doctrine" in pages["meta.about"], "which still carries the retrieval policy"

    async def test_both_puts_one_document_on_two_surfaces(self, tmp_path: Path):
        """`audience: both` is for a document each consumer genuinely needs -- purpose, typically."""
        knowledge_base = _build_knowledge_base(tmp_path)
        (knowledge_base / "_meta" / "purpose.md").write_text("---\naudience: both\n---\n\n# Purpose\n\nWhy it exists.\n", encoding="utf-8")
        (knowledge_base / "_meta" / "schema.md").write_text("---\naudience: authoring\n---\n\n# Schema\n\nGrammar.\n", encoding="utf-8")
        uow = FakeKnowledgeBaseUnitOfWork()
        await _handler(knowledge_base, uow=uow).handle(IndexKnowledgeBase(collection="research"))

        pages = self._orientation(uow)
        assert "Why it exists" in pages["meta.about"]
        assert "Why it exists" in pages["meta.authoring"]

    async def test_the_orientation_carries_prose_never_frontmatter(self, tmp_path: Path):
        """A reader pays for orientation by the token; the sources' YAML bookkeeping is not orientation."""
        knowledge_base = _build_knowledge_base(tmp_path)
        (knowledge_base / "_meta" / "purpose.md").write_text(
            "---\nid: meta.purpose\ntitle: Purpose\nstatus: verified\n---\n\n# Purpose\n\nWhy it exists.\n", encoding="utf-8"
        )
        uow = FakeKnowledgeBaseUnitOfWork()
        await _handler(knowledge_base, uow=uow).handle(IndexKnowledgeBase(collection="research"))

        about = self._orientation(uow)["meta.about"]
        assert "Why it exists" in about
        assert "status: verified" not in about
        assert not about.startswith("---")

    async def test_an_unrecognised_audience_warns_and_does_not_vanish(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        """Never silently narrow on a typo: a document nobody can find on any surface is the worst outcome."""
        knowledge_base = _build_knowledge_base(tmp_path)
        (knowledge_base / "_meta" / "schema.md").write_text("---\naudience: authors\n---\n\n# Schema\n\nGrammar.\n", encoding="utf-8")
        uow = FakeKnowledgeBaseUnitOfWork()
        with caplog.at_level("WARNING"):
            await _handler(knowledge_base, uow=uow).handle(IndexKnowledgeBase(collection="research"))

        assert "audience" in caplog.text
        assert "authors" in caplog.text
        assert "Grammar" in self._orientation(uow)["meta.about"], "it falls back to the default surface, not to nowhere"


class TestOrientationPageCollectionScoping:
    """The synthetic orientation page's id must never collide across collections.

    `kb.pages.id` sits in the composite `(collection, id)` key, but the two orientation ids
    were the SAME literal string for every knowledge base regardless (`meta.about` / `meta.authoring`), so
    a second collection's full rebuild would upsert straight over a first collection's orientation
    row -- proven live: indexing `field-notes` after `repo-docs` left `repo-docs`'s `meta.about`
    row stamped `collection='field-notes'`, so `api.collection_about('repo-docs')` returned nothing.
    `orientation_page_id` now scopes every collection's id, no exception, closing the hole for good.
    """

    async def test_the_default_collection_also_gets_a_scoped_id(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        await _handler(knowledge_base, uow=uow).handle(IndexKnowledgeBase(collection="research"))

        page_ids = {str(row["id"]) for row in uow.pages.upserted_rows}
        assert "meta.about" not in page_ids, "the bare literal id would collide with another collection's row"
        assert "meta.about@research" in page_ids

    async def test_a_non_default_collection_gets_a_scoped_id(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        await _handler(knowledge_base, uow=uow).handle(IndexKnowledgeBase(collection="repo-docs"))

        page_ids = {str(row["id"]) for row in uow.pages.upserted_rows}
        assert "meta.about" not in page_ids, "the bare literal id would collide with another collection's row"
        assert "meta.about@repo-docs" in page_ids

    async def test_two_collections_compiled_into_the_same_store_keep_distinct_orientation_rows(self, tmp_path: Path):
        """The actual bug scenario: two collections' full rebuilds must never target the same id."""
        knowledge_base_a = _build_knowledge_base(tmp_path / "a")
        knowledge_base_b = _build_knowledge_base(tmp_path / "b")
        uow = FakeKnowledgeBaseUnitOfWork()

        await _handler(knowledge_base_a, uow=uow).handle(IndexKnowledgeBase(collection="repo-docs"))
        await _handler(knowledge_base_b, uow=uow).handle(IndexKnowledgeBase(collection="field-notes"))

        about_ids = {str(row["id"]) for row in uow.pages.upserted_rows if str(row["id"]).startswith("meta.about")}
        assert about_ids == {"meta.about@repo-docs", "meta.about@field-notes"}


class TestOrientationPageId:
    """`orientation_page_id` / `orientation_page_ids`: always collection-scoped, no exception."""

    def test_scopes_every_collection_including_the_former_default(self):
        assert orientation_page_id("meta.about", "research") == "meta.about@research"
        assert orientation_page_id("meta.about", "repo-docs") == "meta.about@repo-docs"
        assert orientation_page_id("meta.authoring", "field-notes") == "meta.authoring@field-notes"

    def test_orientation_page_ids_scopes_every_base_id(self):
        assert orientation_page_ids("repo-docs") == {"meta.about@repo-docs", "meta.authoring@repo-docs"}


class TestActiveEmbedStrategiesGateRowBuilding:
    """`KnowledgeBaseSchema.active_embed_strategies()` must be enforced, not merely declarable.

    A deactivated strategy's rows stop indexing entirely -- a search-quality tradeoff the owner
    chooses, exactly as claim/source-chunk's own deliberate scope boundary already worked. `page-card`
    is structurally mandatory (enforced by the spec validator, `TestValidatorRejects` in
    `test_spec.py`), so every page's row and its FK anchor (sections/claims/cells/edges) stay intact
    regardless of what else is deactivated.
    """

    def _spec(self, active: tuple[str, ...]) -> KnowledgeBaseSchema:
        return research_spec().model_copy(update={"embed_strategies": EmbedStrategiesSpec(active=active)})

    async def test_deactivating_section_builds_no_section_rows(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        spec = self._spec(("page-card", "claim", "source-chunk"))

        await _handler(knowledge_base, uow=uow, spec=spec).handle(IndexKnowledgeBase(collection="research"))

        assert uow.sections.upserted_rows == []
        assert uow.pages.upserted_rows != []  # the page row itself is unaffected

    async def test_deactivating_claim_builds_no_claim_rows(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        spec = self._spec(("page-card", "section", "source-chunk"))

        await _handler(knowledge_base, uow=uow, spec=spec).handle(IndexKnowledgeBase(collection="research"))

        assert uow.claims.upserted_rows == []

    async def test_deactivating_source_chunk_builds_no_chunk_rows(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        spec = self._spec(("page-card", "section", "claim"))

        await _handler(knowledge_base, uow=uow, spec=spec).handle(IndexKnowledgeBase(collection="research"))

        assert uow.sources.upserted_chunks == []
        assert uow.sources.sources != []  # sources_raw itself is independent of the chunking strategy

    async def test_a_knowledge_base_declaring_nothing_keeps_every_row_type(self, tmp_path: Path):
        """No explicit override -> today's full set, unchanged (the D3 acceptance proof's own contract)."""
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()

        await _handler(knowledge_base, uow=uow).handle(IndexKnowledgeBase(collection="research"))  # spec=None

        assert uow.sections.upserted_rows != []
        assert uow.claims.upserted_rows != []
        assert uow.sources.upserted_chunks != []


class TestPruneMissing:
    async def test_prune_called_per_aggregate_with_current_ids(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        # Pages pruned with exactly the current page id set (full-rebuild semantics).
        assert uow.pages.pruned == [{"paper.lightrag-2025", "ent.lightrag", "meta.about@research"}]

    async def test_fresh_load_prunes_nothing(self, tmp_path: Path):
        # A index into an empty store deletes no rows: every id the knowledge base produced is in
        # the keep-set, so the prune count is 0 (not the page *count*). This is the Pruned:0
        # contract a no-change re-index must report (Defect 2).
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()  # default: store is empty, so prune_missing deletes nothing
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        assert report.pruned == 0

    async def test_pruned_counts_only_rows_actually_deleted(self, tmp_path: Path):
        # The report's prune count is the number of rows DELETE removed across the pruned
        # aggregates — not the kept count, and not polluted by the wholesale edge rebuild.
        # Seed the page store with one stale id absent from the current knowledge base → exactly 1 deleted.
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.pages.existing = {"paper.lightrag-2025", "ent.lightrag", "meta.about@research", "paper.gone-2099"}
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        assert report.pruned == 1  # only the vanished page; edges' delete-all does not count

    async def test_prune_count_is_emitted_on_pages_loaded(self, tmp_path: Path):
        # The PagesLoaded event carries the same true prune count the report does.
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.claims.existing = {"paper.lightrag-2025#c1", "paper.lightrag-2025#c-stale"}
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        pages_loaded = next(event for event in uow.coordination.events if isinstance(event, PagesLoaded))
        assert pages_loaded.pruned == 1

    async def test_a_summary_whose_unit_the_knowledge_base_no_longer_has_is_pruned(self, tmp_path: Path):
        # A disclosure summary outlives the page or section it describes unless the rebuild prunes
        # it: the keep-set is this index's pages plus its sections, and nothing else survives.
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.summaries.page_units = {"paper.lightrag-2025", "paper.gone-2099"}
        uow.summaries.section_units = {"paper.gone-2099#tl_dr": "paper.gone-2099"}
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        assert uow.summaries.page_units == {"paper.lightrag-2025"}
        assert uow.summaries.section_units == {}
        assert report.pruned == 2
        assert uow.summaries.pruned_collections == ["research"]

    async def test_the_summary_keep_set_is_this_indexs_pages_and_sections(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        page_ids = {str(row["id"]) for row in uow.pages.upserted_rows}
        section_ids = {str(row["id"]) for row in uow.sections.upserted_rows}
        assert uow.summaries.pruned == [page_ids | section_ids]

    async def test_embed_meta_repo_has_no_prune_surface(self):
        # embed_meta is exempt from prune — the recording meta repo offers no prune method.
        assert not hasattr(FakeKnowledgeBaseUnitOfWork().meta, "prune_missing")


class TestParseFaultTolerance:
    async def test_unparseable_note_becomes_lint_error_and_run_continues(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        # A note whose claim omits the required loc:: — the parser raises; the handler flags + skips it.
        KnowledgeBaseBuilder(knowledge_base).add_unit_document(
            "Wiki/Papers",
            "broken",
            "---\nid: paper.broken\ntype: paper\ntitle: Broken\nstatus: draft\n"
            "zotero_key: K\nyear: 2025\npaper_type: method\nsource_hash: h\n---\n\n"
            "## Findings\n\n- A claim with no locator. ^c1\n    - kind:: observation\n",
        )
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        assert any(issue.rule == "parse-error" for issue in report.validation.errors)
        # The two good notes still loaded despite the broken one (+ the meta.about page).
        assert {row["id"] for row in uow.pages.upserted_rows} == {"paper.lightrag-2025", "ent.lightrag", "meta.about@research"}

    async def test_validation_errors_still_load_rows(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        # A paper with an unregistered claim type → a validation ERROR, but the row must still load.
        KnowledgeBaseBuilder(knowledge_base).add_unit_document(
            "Wiki/Papers",
            "flagged",
            "---\nid: paper.flagged\ntype: paper\ntitle: Flagged\nstatus: draft\n"
            "zotero_key: K2\nyear: 2025\npaper_type: method\nsource_hash: h\n---\n\n"
            "## Findings\n\n- A claim of a bogus page_type. ^c1\n    - loc:: §1\n    - kind:: not-a-real-page_type\n",
        )
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        assert report.validation.n_errors >= 1
        assert "paper.flagged" in {row["id"] for row in uow.pages.upserted_rows}


async def _deliver(bus: MessageBus) -> None:
    """Deliver what the bus put in its in-memory outbox, the way the worker does in production."""
    outbox = bus.outbox
    assert isinstance(outbox, MemoryOutbox)
    await Worker(jobs=MemoryJobs(), outbox=outbox, bus=bus, wake=ImmediateWake(), name="test").deliver_once()


class TestEventSequence:
    async def test_emits_the_pipeline_events_in_order_with_embed(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research", embed=True))

        assert [type(event) for event in handler.events] == [
            IndexingStarted,
            NotesParsed,
            ValidationCompleted,
            EmbeddingsRefreshed,
            IndexingCompleted,
        ]

    async def test_the_load_transaction_carries_what_it_wrote(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research", embed=True))

        assert [type(event) for event in uow.coordination.events] == [PagesLoaded, CitingPagesChanged]

    async def test_the_load_transaction_names_the_collection_it_wrote(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="lab", embed=True))

        loaded, changed = uow.coordination.events
        assert isinstance(loaded, PagesLoaded)
        assert isinstance(changed, CitingPagesChanged)
        assert (loaded.collection, changed.collection) == ("lab", "lab")

    async def test_no_embed_omits_embeddings_refreshed(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert [type(event) for event in handler.events] == [IndexingStarted, NotesParsed, ValidationCompleted, IndexingCompleted]

    async def test_bus_drains_events_to_subscribers(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)
        spy = BusSpy()
        bus = MessageBus({IndexKnowledgeBase: handler}, {IndexingCompleted: [spy], NotesParsed: [spy]})

        await bus.handle(IndexKnowledgeBase(collection="research", embed=False))
        await _deliver(bus)

        # The worker delivered the handler's events to the matching subscribers.
        received = [type(event) for event in spy.received]
        assert NotesParsed in received
        assert IndexingCompleted in received


class TestDashboardWriteBack:
    async def test_sink_receives_the_lint_report(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        validation_report_sink = FakeValidationReportSink()
        handler = _handler(knowledge_base, uow=uow, validation_report_sink=validation_report_sink)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        assert len(validation_report_sink.written) == 1
        assert validation_report_sink.written[0] is report.validation


class TestFullCompileStatePopulation:
    """The full path ALSO writes the four index-state tables.

    A full rebuild keeps today's whole-set prune / `replace_all` / `refresh_graph` AND, inside the
    same load transaction, populates `index_manifest` (one entry per file), `link_refs` (every
    outbound target, resolved + dangling), `validation_store` (every emitted issue), and a single
    `index_fingerprint` row. `IndexReport.mode` is `"full"`.
    """

    async def test_manifest_has_an_entry_per_compiled_file(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        entries = [row for batch in uow.manifest.manifest_upserts for row in batch]
        by_path = {str(entry["path"]): entry for entry in entries}
        # One note entry per memory note + one source entry per mirror.
        assert "Wiki/Papers/lightrag-2025.md" in by_path
        assert "Wiki/Entities/lightrag.md" in by_path
        assert "Sources/lightrag-2025.md" in by_path
        paper = by_path["Wiki/Papers/lightrag-2025.md"]
        assert paper["kind"] == "note"
        assert paper["page_id"] == "paper.lightrag-2025"
        assert paper["source_key"] is None
        assert str(paper["content_hash"])  # non-empty raw-file hash
        assert str(paper["identity_hash"])  # non-empty {stem,id,title,aliases} hash
        mirror = by_path["Sources/lightrag-2025.md"]
        assert mirror["kind"] == "source"
        assert mirror["page_id"] is None
        assert mirror["source_key"] == "8QXFXYMI"
        # The whole-set write deletes no manifest paths (the NOT-IN prune is the baseline).
        assert uow.manifest.deleted_paths == []

    async def test_a_full_rebuild_drops_manifest_rows_for_paths_it_did_not_read(self, tmp_path: Path):
        """A row left by a file that moved away would read as a removed file on every later incremental run."""
        from engram.indexing.contracts import ManifestEntry
        from engram.indexing.domain.manifest import FileType

        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        stale = "Archive/lightrag-2025.md"
        uow.manifest.stored = {
            stale: ManifestEntry(
                path=stale, kind=FileType.NOTE, content_hash="old", identity_hash="old", page_id="paper.lightrag-2025", source_key=None
            )
        }
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research", full=True))

        assert uow.manifest.deleted_paths == [{stale}]
        assert "Wiki/Papers/lightrag-2025.md" in {str(row["path"]) for batch in uow.manifest.manifest_upserts for row in batch}

    async def test_full_rebuild_writes_one_contract_row_per_spec_page_type(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        spec = research_spec()
        handler = _handler(knowledge_base, uow=uow, spec=spec)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        rows = uow.page_type_contracts.upserted_rows
        by_page_type = {row["page_type"]: row for row in rows}
        assert set(by_page_type) == {pt.page_type for pt in spec.page_types}
        flag_contract = cast("dict[str, object]", by_page_type["flag"]["contract"])
        assert flag_contract["page_type"] == "flag"
        assert flag_contract["status_values"] == ("pending", "accepted", "dismissed", "completed")
        assert all(row["collection"] == "research" for row in rows)
        assert uow.page_type_contracts.pruned == [{pt.page_type for pt in spec.page_types}]
        assert uow.page_type_contracts.pruned_collections == ["research"]

    async def test_link_refs_include_resolved_and_dangling_targets(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        rows = [row for batch in uow.link_refs.link_ref_rows for row in batch]
        assert rows, "the full path must write the outbound link-ref graph"
        # `proposes:: [[lightrag]]` resolves to a real loaded page; the claim's [[graphrag]] dangles.
        loaded_ids = {str(row["id"]) for row in uow.pages.upserted_rows}
        resolved = [row for row in rows if row["dst_id"] is not None]
        dangling = [row for row in rows if row["dst_id"] is None]
        assert resolved, "a resolved target (dst_id set) must be persisted"
        assert all(str(row["dst_id"]) in loaded_ids for row in resolved)
        assert any(row["raw_target"] == "lightrag" and row["rel"] == "proposes" for row in resolved)
        assert dangling, "a dangling target (dst_id is None) must be persisted"
        assert any(row["raw_target"] == "graphrag" for row in dangling)
        # Targets are lower-cased and the rows carry their source page + relation.
        assert all(str(row["raw_target"]) == str(row["raw_target"]).lower() for row in rows)
        assert all(row["src_page_id"] for row in rows)
        # `dst_collection` is a citation paired with `dst_id`: set when resolved, None when dangling.
        assert all(row["dst_collection"] == "research" for row in resolved)
        assert all(row["dst_collection"] is None for row in dangling)

    async def test_every_child_row_carries_its_collection(self, tmp_path: Path):
        """Sections/claims/edges all gain `collection` (and edges gain the paired `dst_collection`)."""
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        sections = [row for batch in uow.sections.upserts for row in batch]
        claims = [row for batch in uow.claims.upserts for row in batch]
        edges = [row for batch in uow.edges.replaced for row in batch]
        assert sections, "the fixture knowledge base must produce section rows"
        assert claims, "the fixture knowledge base must produce claim rows"
        assert edges, "the fixture knowledge base must produce edge rows"
        assert all(row["collection"] == "research" for row in sections)
        assert all(row["collection"] == "research" for row in claims)
        assert all(row["src_collection"] == "research" and row["dst_collection"] == "research" for row in edges)

    async def test_validation_store_gets_a_row_per_emitted_issue(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        # A paper with an unregistered claim type → one validation ERROR that must be persisted.
        KnowledgeBaseBuilder(knowledge_base).add_unit_document(
            "Wiki/Papers",
            "flagged",
            "---\nid: paper.flagged\ntype: paper\ntitle: Flagged\nstatus: draft\n"
            "zotero_key: K2\nyear: 2025\npaper_type: method\nsource_hash: h\n---\n\n"
            "## Findings\n\n- A claim of a bogus page_type. ^c1\n    - loc:: §1\n    - kind:: not-a-real-page_type\n",
        )
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        stored = [row for batch in uow.validation_store.validation_rows for row in batch]
        # Every emitted error + warning is persisted, one row each.
        assert len(stored) == report.validation.n_errors + report.validation.n_warnings
        assert any(row["rule"] == "unknown-kind" and row["page_id"] == "paper.flagged" for row in stored)
        assert all(row["page_id"] for row in stored)

    async def test_a_provider_less_mirror_persists_a_validation_row_and_is_not_loaded(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        KnowledgeBaseBuilder(knowledge_base).add_source_mirror("orphan", _ORPHAN_MIRROR)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        assert report.validation.n_errors >= 1
        stored = [row for batch in uow.validation_store.validation_rows for row in batch]
        assert any(row["rule"] == "mirror-without-provider" and row["page_id"] == "Sources/orphan.md" for row in stored)
        loaded_ids = {str(row["source_id"]) for batch in uow.sources.sources for row in batch}
        assert "ORPHAN1" not in loaded_ids

    async def test_full_rebuild_replaces_the_whole_collections_lint_not_only_pages_with_issues(self, tmp_path: Path):
        """A row this rebuild did not emit again is a fixed issue; keeping it would show a ghost forever."""
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        assert uow.validation_store.replaced_all == ["research"]
        assert uow.validation_store.replaced_page_ids == []

    async def test_fingerprint_is_written_once_with_version_and_model(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        embedder = FakeEmbedding(model="fake-embed", dimensions=1024)
        handler = _handler(knowledge_base, uow=uow, embedder=embedder)

        await handler.handle(IndexKnowledgeBase(collection="research"))

        fingerprint = uow.fingerprint.fingerprint
        assert fingerprint is not None  # exactly one fingerprint row was set
        assert fingerprint["collection"] == "research"
        assert fingerprint["indexer_version"] == indexer_version(DEFAULT_CHUNK_SIZES)
        assert fingerprint["embed_model"] == "fake-embed"
        assert fingerprint["spec_hash"]  # non-empty
        assert fingerprint["vocabulary_hash"]  # non-empty

    async def test_report_mode_is_full(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research"))

        assert report.mode == "full"

    async def test_state_writes_share_the_single_load_transaction(self, tmp_path: Path):
        # The four writes land INSIDE the one load transaction: the manifest / link_refs / validation /
        # fingerprint are recorded before the same commit the rows use.
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert uow.committed is True
        assert uow.manifest.manifest_upserts  # state was written, not skipped
        assert uow.link_refs.link_ref_rows
        assert uow.fingerprint.fingerprint is not None


def _seed_stored_state_from_full_run(uow: FakeKnowledgeBaseUnitOfWork) -> None:
    """Promote the last full run's written manifest + fingerprint into the fake's stored view.

    A full index writes `index_manifest` rows (`manifest.manifest_upserts`) and one
    `index_fingerprint` (`fingerprint.fingerprint`); the next incremental run reads them back
    via `manifest.load` (`manifest.stored`) / `fingerprint.get` (`fingerprint.stored`). The fake
    keeps writes and reads in separate attributes, so a test promotes one to the other to simulate
    "the prior index is on disk". The manifest entries are mapped to `ManifestEntry` (the shape
    `load` yields), keyed by path.
    """
    from engram.indexing.contracts import ManifestEntry
    from engram.indexing.domain.manifest import FileType

    stored: dict[str, ManifestEntry] = {}
    for batch in uow.manifest.manifest_upserts:
        for entry in batch:
            path = str(entry["path"])
            source_key = entry["source_key"]
            page_id = entry["page_id"]
            stored[path] = ManifestEntry(
                path=path,
                kind=FileType(str(entry["kind"])),
                content_hash=str(entry["content_hash"]),
                identity_hash=str(entry["identity_hash"]),
                page_id=str(page_id) if page_id is not None else None,
                source_key=str(source_key) if source_key is not None else None,
            )
    uow.manifest.stored = stored
    written = uow.fingerprint.fingerprint
    assert written is not None
    uow.fingerprint.stored = StoredFingerprint(**{key: str(value) for key, value in written.items() if key not in {"spec", "collection"}})


def _clean_pages_from_full_run(uow: FakeKnowledgeBaseUnitOfWork) -> list[CleanPage]:
    """Build the clean-page view production would read from `kb.pages` + the manifest.

    `kb.pages` carries each page's resolution keys (id/slug/title/aliases) + its `source_id`;
    the manifest carries the file path. The handler joins them into the `CleanPage` rows the
    incremental run's merged index + mirror-citer lookup consume. Here the join is reconstructed
    from the recorded full-run upserts so a handler-level incremental run has a faithful clean view.
    """
    path_by_page = {
        str(entry["page_id"]): str(entry["path"]) for batch in uow.manifest.manifest_upserts for entry in batch if entry["page_id"]
    }
    pages: list[CleanPage] = []
    for row in uow.pages.upserted_rows:
        page_id = str(row["id"])
        if page_id not in path_by_page:
            continue
        aliases = row["aliases"]
        source_id = row.get("source_id")
        pages.append(
            CleanPage(
                id=page_id,
                slug=str(row["slug"]),
                title=str(row["title"]),
                aliases=tuple(str(alias) for alias in aliases) if isinstance(aliases, (list, tuple)) else (),
                path=path_by_page[page_id],
                source_id=str(source_id) if source_id else None,
            )
        )
    return pages


class TestFullVsIncrementalBranch:
    """`handle()` routes the full vs incremental path.

    The FULL path runs when `cmd.full` is set, OR the stored `index_fingerprint` differs from the
    current `(INDEXER_VERSION, spec_hash, vocabulary_hash, embed_model)`, OR the manifest is empty
    (cold start). Otherwise the incremental path runs. `IndexReport.mode` reflects the path taken.
    """

    async def test_full_flag_forces_the_full_path(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)
        # Seed a matching fingerprint + manifest so only `full=True` could force the full path.
        await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
        _seed_stored_state_from_full_run(uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False, full=True))

        assert report.mode == "full"

    async def test_empty_manifest_cold_start_runs_full(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()  # default: empty manifest + no fingerprint → cold start
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert report.mode == "full"

    async def test_fingerprint_mismatch_runs_full(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)
        await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
        _seed_stored_state_from_full_run(uow)
        # A stale embed model in the stored fingerprint ⇒ a global invalidator ⇒ full rebuild.
        stale = uow.fingerprint.stored
        assert isinstance(stale, StoredFingerprint)
        uow.fingerprint.stored = replace(stale, embed_model="some-old-model")

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert report.mode == "full"

    async def test_an_index_written_before_mirror_chunks_were_capped_runs_full(self, tmp_path: Path):
        # Indexer version "1" wrote one raw chunk per mirror heading however long its body was. An
        # unchanged mirror is never re-chunked incrementally, so that index must rebuild in full.
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)
        await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
        _seed_stored_state_from_full_run(uow)
        clean = _clean_pages_from_full_run(uow)
        handler.set_clean_page_view(lambda: clean)
        stored = uow.fingerprint.stored
        assert isinstance(stored, StoredFingerprint)
        uow.fingerprint.stored = replace(stored, indexer_version="1")

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert report.mode == "full"

    async def test_match_plus_body_edit_runs_incremental(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)
        await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
        _seed_stored_state_from_full_run(uow)
        clean = _clean_pages_from_full_run(uow)
        handler.set_clean_page_view(lambda: clean)  # production reads this from kb.pages
        # Edit one note's body only (its identity keys are unchanged) → the incremental path.
        KnowledgeBaseBuilder(knowledge_base).add_unit_document("Wiki/Entities", "lightrag", _ENTITY.replace("graph-indexed", "graph-indexed (revised)"))

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert report.mode == "incremental"

    async def test_incremental_skips_the_unchanged_pages(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)
        await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
        _seed_stored_state_from_full_run(uow)
        clean = _clean_pages_from_full_run(uow)
        handler.set_clean_page_view(lambda: clean)
        KnowledgeBaseBuilder(knowledge_base).add_unit_document("Wiki/Entities", "lightrag", _ENTITY.replace("graph-indexed", "graph-indexed (revised)"))

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert report.skipped > 0  # the other manifest entries stayed clean


class TestDryRun:
    """`cmd.dry_run` computes + returns the dirty-set preview but writes nothing.

    The dirty-set preview is produced (the per-partition counts + the re-embed / edges /
    graph-refresh signals), but no upsert/delete/commit reaches the UoW, and the handler attaches
    the preview to the report. A cold-start knowledge base (empty manifest fake) previews what a real run
    would do — go FULL — rather than crashing.
    """

    async def test_dry_run_writes_nothing_to_the_uow(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research", embed=False, dry_run=True))

        assert uow.committed is False
        assert uow.pages.upserts == []
        assert uow.pages.pruned == []
        assert uow.edges.replaced == []
        assert uow.manifest.manifest_upserts == []
        assert uow.fingerprint.fingerprint is None

    async def test_dry_run_attaches_a_preview_to_the_report(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(knowledge_base, uow=uow)

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False, dry_run=True))

        assert isinstance(report, IndexReport)
        # The preview is the dry-run deliverable: a cold start (empty manifest fake) would go FULL, so
        # every tracked file is in the reparse partition and the graph would refresh.
        assert report.dry_run is not None
        assert report.dry_run.content_changed.count + report.dry_run.added.count > 0
        assert report.dry_run.graph_refresh is True


class TestDanglingRelationsStayOutOfTheGraph:
    """`kb.edges` is the resolved-only graph; the dangling half belongs to `kb.link_refs`."""

    @staticmethod
    def _knowledge_base_with_a_dangling_relation(tmp_path: Path) -> Path:
        """The mini knowledge base, with one unresolvable `builds_on` added to the paper's frontmatter."""
        dangling = _PAPER.replace("proposes:", 'builds_on: ["[[nothing-here]]"]\nproposes:')
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_unit_document("Wiki/Papers", "lightrag-2025", dangling)
        builder.add_unit_document("Wiki/Entities", "lightrag", _ENTITY)
        builder.add_source_mirror("lightrag-2025", _MIRROR)
        builder.add_vocabulary()
        builder.add_meta_stubs()
        return tmp_path

    async def test_a_dangling_frontmatter_relation_writes_no_edge(self, tmp_path: Path):
        # Unfiltered, the resolver's `dst_id=None` edge stringifies to `dst_id='None'` — one
        # phantom page id every dangling relation in the knowledge base points at, which `refresh_graph`
        # then projects into `kb_graph` as a real node.
        knowledge_base = self._knowledge_base_with_a_dangling_relation(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()

        await _handler(knowledge_base, uow=uow).handle(IndexKnowledgeBase(collection="research", embed=False))

        edges = [row for batch in uow.edges.replaced for row in batch]
        assert edges, "the resolvable relations should still be loaded"
        assert [row for row in edges if row["dst_id"] in {"None", None}] == []

    async def test_the_dangling_target_is_still_recorded_as_a_link_ref(self, tmp_path: Path):
        # Dropping it from `kb.edges` must not lose it: `link_refs` is what the inbound closure
        # and the ingest queue read, and both need the dangling target by name.
        knowledge_base = self._knowledge_base_with_a_dangling_relation(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()

        await _handler(knowledge_base, uow=uow).handle(IndexKnowledgeBase(collection="research", embed=False))

        refs = [row for batch in uow.link_refs.link_ref_rows for row in batch]
        assert "nothing-here" in {str(row["raw_target"]) for row in refs}


class TestDuration:
    async def test_duration_comes_from_the_injected_clock(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        # A clock that ticks 10.0 then 12.5 → a 2.5s measured duration.
        ticks = iter([10.0, 12.5])
        handler = IndexKnowledgeBaseHandler(
            FilesystemKnowledgeBaseReader(knowledge_base, note_folders=research_spec().note_folders),
            FakeEmbedding(),
            lambda: uow,
            FakeValidationReportSink(),
            spec=research_spec(),
            clock=lambda: next(ticks),
        )

        report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert report.duration_seconds == pytest.approx(2.5)
