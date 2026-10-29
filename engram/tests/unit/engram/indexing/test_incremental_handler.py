"""Unit tests for the incremental index orchestration (`src/engram/indexing/service/incremental.py`).

These pin the **happy path** of `run_incremental`:
a fingerprint match + a non-empty manifest (a hit) + exactly one body-only edit (one
`content_changed`, no `identity_changed` / add / remove). The body-only edit's inbound
closure is empty (the completeness lemma), so the dirty set is just that one file.

Everything runs over `FakeKnowledgeBaseUnitOfWork` (recording repos) + a `FakeKnowledgeBaseReader` +
fakes for the clean-DB page view and the embedder, so there is no database, no network,
and no real filesystem. They assert, for a single body-only edit:

* exactly ONE page is upserted (the dirty page) — clean pages are untouched;
* ZERO deletes (no vanished/renamed-away rows on a body-only edit);
* `refresh_graph` is NOT called (edge-set re-projection is gated to edge changes);
* `link_refs` / `validation_store` are replaced only for the dirty page;
* `CitingPagesChanged` carries only that paper's id (empty when the dirty page is not a paper);
* `report.mode == "incremental"` and `report.skipped == N - 1` (the manifest's other entries).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from engram.indexing.contracts import (
    CitingPagesChanged,
    IndexKnowledgeBase,
    KnowledgeBaseReader,
    ManifestEntry,
    PagesLoaded,
    ValidationIssue,
    ValidationReport,
    content_hash,
)
from engram.indexing.domain.manifest import FileType, identity_hash
from engram.indexing.read_models import CleanPage, PendingEmbedRow, StoredFingerprint
from engram.indexing.service.incremental import run_incremental
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork, _RecordingRepo
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from core.ai.embedding import BatchEmbedder

    from engram.indexing.internal_contracts import IncrementalDeps, IncrementalUnitOfWork
    from engram.kb.contracts import KnowledgeBaseSchema

# A well-formed paper note. Its frontmatter id/title/aliases (the identity surface) are the
# resolution keys; a body-only edit changes the section prose without touching any of them.
_PAPER_BODY_V1 = "LightRAG indexes a knowledge base as a graph and retrieves at two levels."
_PAPER_BODY_V2 = "LightRAG indexes a knowledge base as a graph and retrieves at two levels (revised)."
_PAPER_PATH = "Memory/Papers/lightrag-2025.md"
_PAPER_ID = "paper.lightrag-2025"


def _paper_note(body: str) -> str:
    return (
        "---\n"
        f"id: {_PAPER_ID}\n"
        "type: paper\n"
        "title: LightRAG\n"
        "summary: Graph-indexed RAG.\n"
        "status: synthesized\n"
        "zotero_key: 8QXFXYMI\n"
        "year: 2025\n"
        "paper_type: method\n"
        "source_hash: bd72\n"
        "---\n\n"
        "## TL;DR\n\n"
        f"{body}\n"
    )


# A second untouched note: it stays clean (in the manifest, never re-parsed) so a body-only
# edit of the paper leaves it skipped. It is also a clean-DB page for the merged index.
_ENTITY_PATH = "Memory/Entities/lightrag.md"
_ENTITY_ID = "ent.lightrag"
_ENTITY_NOTE = (
    "---\n"
    f"id: {_ENTITY_ID}\n"
    "type: entity\n"
    "title: LightRAG\n"
    "status: draft\n"
    "entity_type: technique\n"
    "---\n\n"
    "## Definition\n\nA graph-indexed retrieval technique.\n"
)


def _note_entry(path: str, page_id: str, *, content: str, title: str, slug: str) -> ManifestEntry:
    """A stored manifest row for a note: its content hash + identity hash + page id."""
    return ManifestEntry(
        path=path,
        kind=FileType.NOTE,
        content_hash=content,
        identity_hash=identity_hash(stem=slug, id_=page_id, title=title, aliases=()),
        page_id=page_id,
        source_key=None,
    )


@dataclass(frozen=True)
class _Deps:
    """A hand-built `IncrementalDeps` for the happy-path test (every collaborator a fake).

    The collaborator fields are typed as the production Protocols (`KnowledgeBaseReader`,
    `BatchEmbedder`, `KnowledgeBaseSchema`) so the dataclass structurally satisfies the
    invariant `IncrementalDeps` properties; the fakes are passed in at construction.
    """

    uow_factory: Callable[[], IncrementalUnitOfWork]
    kb_reader: KnowledgeBaseReader
    embedder: BatchEmbedder
    clean_pages: tuple[CleanPage, ...]
    indexer_version: str = "engram-index/1"
    spec_hash: str = "spec-hash-1"
    vocabulary_hash: str = "vocabulary-hash-1"
    spec: KnowledgeBaseSchema = field(default_factory=research_spec)
    path_root: Path | None = None
    path_root_source: str | None = None

    def clean_page_view(self) -> Sequence[CleanPage]:
        return self.clean_pages


def _clean_paper() -> CleanPage:
    return CleanPage(id=_PAPER_ID, slug="lightrag-2025", title="LightRAG", aliases=(), path=_PAPER_PATH)


def _clean_entity() -> CleanPage:
    return CleanPage(id=_ENTITY_ID, slug="lightrag", title="LightRAG", aliases=(), path=_ENTITY_PATH)


def _build_deps(
    uow: FakeKnowledgeBaseUnitOfWork,
    *,
    paper_body: str,
    embedder: FakeEmbedding | None = None,
    paper_text: str | None = None,
    mirrors: Sequence[tuple[str, str]] = (),
) -> _Deps:
    """Script a body-only edit: the manifest carries both notes, the paper's body is V2 on disk.

    The stored fingerprint matches `_Deps`'s inputs (so the run stays incremental), the stored
    manifest has both notes (N = 2 entries), and the paper's `content_hash` differs from the
    stored entry while its `identity_hash` (id/title/aliases/stem) is unchanged. A caller that
    needs to assert on the embedder's recorder passes its own concrete `FakeEmbedding` in; one
    that needs the paper's on-disk text to be something other than a well-formed note passes
    `paper_text`; one that needs an extra `Sources/` mirror on disk (with no stored manifest
    entry, so it lands in `diff.added`) passes `mirrors`.
    """
    on_disk = paper_text if paper_text is not None else _paper_note(paper_body)
    reader = FakeKnowledgeBaseReader(units=[(_PAPER_PATH, on_disk), (_ENTITY_PATH, _ENTITY_NOTE)], sources=mirrors)
    embedder = embedder if embedder is not None else FakeEmbedding()

    # Stored manifest: both notes, with the paper recorded at its V1 content hash.
    uow.manifest.stored = {
        _PAPER_PATH: _note_entry(
            _PAPER_PATH, _PAPER_ID, content=content_hash(_paper_note(_PAPER_BODY_V1)), title="LightRAG", slug="lightrag-2025"
        ),
        _ENTITY_PATH: _note_entry(_ENTITY_PATH, _ENTITY_ID, content=content_hash(_ENTITY_NOTE), title="LightRAG", slug="lightrag"),
    }
    # Stored fingerprint matches the deps inputs → incremental (no global invalidation).
    uow.fingerprint.stored = StoredFingerprint(
        indexer_version="engram-index/1", spec_hash="spec-hash-1", vocabulary_hash="vocabulary-hash-1", embed_model=embedder.model_id
    )
    return _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=(_clean_paper(), _clean_entity()))


class TestBodyOnlyEditHappyPath:
    async def test_only_the_dirty_page_is_upserted(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        page_ids = {row["id"] for row in uow.pages.upserted_rows}
        assert page_ids == {_PAPER_ID}  # the entity (clean) is NOT re-upserted

    async def test_dirty_section_is_upserted_with_a_derived_summary(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        tldr = next(row for row in uow.sections.upserted_rows if row["id"] == f"{_PAPER_ID}#tl_dr")
        assert tldr["summary"] == _PAPER_BODY_V2  # short body, under the inline gate -> verbatim

    async def test_no_deletes_on_a_body_only_edit(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.pages.pruned == []  # no targeted page delete
        assert uow.manifest.deleted_paths == []  # no manifest path removed

    async def test_refresh_graph_is_not_called(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.edges.refreshed == 0  # edge re-projection is gated to edge changes

    async def test_link_refs_and_lint_replaced_only_for_the_dirty_page(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.link_refs.replaced_src_ids == [{_PAPER_ID}]
        # The validation clear-set is the dirty page plus its own file path (a parse error is keyed by
        # path), never a clean page's id.
        assert len(uow.validation_store.replaced_page_ids) == 1
        cleared = uow.validation_store.replaced_page_ids[0]
        assert _PAPER_ID in cleared
        assert any(key.endswith(".md") for key in cleared)
        assert all(key == _PAPER_ID or key.endswith(".md") for key in cleared)

    async def test_the_load_transaction_records_the_dirty_papers_id(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        changed = next(event for event in uow.coordination.events if isinstance(event, CitingPagesChanged))
        assert changed.page_ids == (_PAPER_ID,)
        assert report.mode == "incremental"

    async def test_the_load_transaction_re_stamps_the_fingerprint_with_the_knowledge_base_root(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.fingerprint.touch_calls == [("research", "/fake/knowledge_base")]

    async def test_page_type_contracts_are_written_on_a_body_only_edit_too(self) -> None:
        """The spec is always parsed, so an incremental run keeps the page-type contracts current, not only a full rebuild."""
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        rows = uow.page_type_contracts.upserted_rows
        assert {row["page_type"] for row in rows} == {pt.page_type for pt in deps.spec.page_types}
        assert all(row["collection"] == "research" for row in rows)
        assert uow.page_type_contracts.pruned == [{pt.page_type for pt in deps.spec.page_types}]

    async def test_the_load_transaction_records_what_it_wrote(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert [type(event) for event in uow.coordination.events] == [PagesLoaded, CitingPagesChanged]
        loaded = uow.coordination.events[0]
        assert isinstance(loaded, PagesLoaded)
        assert loaded.pages == 1
        assert loaded.pruned == 0

    async def test_the_load_transaction_names_the_collection_it_wrote(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="lab"), deps=deps)

        loaded, changed = uow.coordination.events
        assert isinstance(loaded, PagesLoaded)
        assert isinstance(changed, CitingPagesChanged)
        assert (loaded.collection, changed.collection) == ("lab", "lab")

    async def test_report_skips_all_but_the_dirty_page(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert report.mode == "incremental"
        assert report.skipped == 1  # N - 1: the entity stayed clean
        assert report.pages == 1  # only the dirty page loaded

    async def test_manifest_entry_updated_for_the_dirty_page(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        upserted_paths = {str(entry["path"]) for batch in uow.manifest.manifest_upserts for entry in batch}
        assert upserted_paths == {_PAPER_PATH}

    async def test_transaction_is_committed(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.committed is True

    def test_deps_dataclass_satisfies_the_incremental_deps_protocol(self) -> None:
        # The hand-built `_Deps` must structurally satisfy the `IncrementalDeps` port the
        # orchestration depends on: a static + runtime check.
        uow = FakeKnowledgeBaseUnitOfWork()
        deps: IncrementalDeps = _build_deps(uow, paper_body=_PAPER_BODY_V2)
        assert deps.indexer_version == "engram-index/1"


# A paper that links (frontmatter `builds_on`) at the entity by the entity's filename stem, so it
# carries a real resolved edge — the load inserts at least one edge, which gates the graph refresh.
_LINKER_PATH = "Memory/Papers/lightrag-2025.md"
_LINKER_ID = "paper.lightrag-2025"


def _linker_paper(target_stem: str) -> str:
    return (
        "---\n"
        f"id: {_LINKER_ID}\n"
        "type: paper\n"
        "title: LightRAG\n"
        "summary: Graph-indexed RAG.\n"
        "status: synthesized\n"
        "zotero_key: 8QXFXYMI\n"
        "year: 2025\n"
        "paper_type: method\n"
        "source_hash: bd72\n"
        f'builds_on: "[[{target_stem}]]"\n'
        "---\n\n"
        "## TL;DR\n\nLightRAG indexes a knowledge base as a graph and retrieves at two levels.\n"
    )


# --- the renamed entity: old file `lightrag.md` (id ent.lightrag) is gone; a NEW file
# `graph-rag.md` (id ent.graph-rag) takes its place. The paper's `[[lightrag]]` link, unchanged on
# disk, must re-resolve (now dangles) and the old entity id must be deleted with its derived rows.
_OLD_ENTITY_PATH = "Memory/Entities/lightrag.md"
_OLD_ENTITY_ID = "ent.lightrag"
_OLD_ENTITY_SLUG = "lightrag"
_NEW_ENTITY_PATH = "Memory/Entities/graph-rag.md"
_NEW_ENTITY_ID = "ent.graph-rag"
_NEW_ENTITY_SLUG = "graph-rag"


def _entity_note(page_id: str, title: str) -> str:
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: entity\n"
        f"title: {title}\n"
        "status: draft\n"
        "entity_type: technique\n"
        "---\n\n"
        "## Definition\n\nA graph-indexed retrieval technique.\n"
    )


def _build_rename_deps(uow: FakeKnowledgeBaseUnitOfWork) -> _Deps:
    """Script a rename: the entity file `lightrag.md` (ent.lightrag) becomes `graph-rag.md` (ent.graph-rag).

    The manifest carries the old entity at its old path/id; disk now has the new file instead. The
    paper (`builds_on: [[lightrag]]`) is unchanged on disk but is the inbound linker the closure must
    re-resolve. The closure query is scripted on the fake link-ref repo to return the paper for the
    affected name. So: old path `removed`, new path `added`, old entity id `deleted`, paper re-parsed.
    """
    reader = FakeKnowledgeBaseReader(
        units=[(_LINKER_PATH, _linker_paper(_OLD_ENTITY_SLUG)), (_NEW_ENTITY_PATH, _entity_note(_NEW_ENTITY_ID, "GraphRAG"))]
    )
    embedder = FakeEmbedding()

    uow.manifest.stored = {
        _LINKER_PATH: _note_entry(
            _LINKER_PATH, _LINKER_ID, content=content_hash(_linker_paper(_OLD_ENTITY_SLUG)), title="LightRAG", slug="lightrag-2025"
        ),
        _OLD_ENTITY_PATH: _note_entry(
            _OLD_ENTITY_PATH,
            _OLD_ENTITY_ID,
            content=content_hash(_entity_note(_OLD_ENTITY_ID, "LightRAG")),
            title="LightRAG",
            slug=_OLD_ENTITY_SLUG,
        ),
    }
    uow.fingerprint.stored = StoredFingerprint(
        indexer_version="engram-index/1", spec_hash="spec-hash-1", vocabulary_hash="vocabulary-hash-1", embed_model=embedder.model_id
    )
    # The inbound closure: the paper references the disappeared name `lightrag`, so the name query
    # returns the paper's id (the unchanged file the run must re-parse + re-resolve).
    uow.link_refs.referencing = {_LINKER_ID}
    # The paper had a resolved edge to the old entity in the prior index; the per-source edge
    # delete removes it (an edge delta) even though it now re-resolves to nothing.
    uow.edges.existing_src_ids = {_LINKER_ID}

    clean = (
        CleanPage(id=_LINKER_ID, slug="lightrag-2025", title="LightRAG", aliases=(), path=_LINKER_PATH),
        CleanPage(id=_OLD_ENTITY_ID, slug=_OLD_ENTITY_SLUG, title="LightRAG", aliases=(), path=_OLD_ENTITY_PATH),
    )
    return _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=clean)


class TestRenameStructuralPath:
    async def test_old_page_id_is_in_the_targeted_delete(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_rename_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert _OLD_ENTITY_ID in {pid for batch in uow.pages.deleted_ids for pid in batch}

    async def test_new_page_is_upserted(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_rename_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        page_ids = {row["id"] for row in uow.pages.upserted_rows}
        assert _NEW_ENTITY_ID in page_ids
        assert _OLD_ENTITY_ID not in page_ids  # the renamed-away id is deleted, not re-upserted

    async def test_inbound_linker_is_reresolved(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_rename_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        # The unchanged paper that pointed at the old name is re-parsed + re-loaded (its link now dangles).
        page_ids = {row["id"] for row in uow.pages.upserted_rows}
        assert _LINKER_ID in page_ids

    async def test_old_id_edges_link_refs_and_lint_are_cleared(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_rename_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        # The dirty ∪ deleted src-id set every per-source replacement clears must include the old id.
        edge_cleared = {sid for batch in uow.edges.replaced_src_ids for sid in batch}
        link_cleared = {sid for batch in uow.link_refs.replaced_src_ids for sid in batch}
        validation_cleared = {pid for batch in uow.validation_store.replaced_page_ids for pid in batch}
        assert _OLD_ENTITY_ID in edge_cleared
        assert _OLD_ENTITY_ID in link_cleared
        assert _OLD_ENTITY_ID in validation_cleared

    async def test_old_manifest_path_is_deleted(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_rename_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert _OLD_ENTITY_PATH in {path for batch in uow.manifest.deleted_paths for path in batch}

    async def test_transaction_is_committed(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_rename_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.committed is True


class TestEdgesGatedRefresh:
    async def test_refresh_graph_is_called_when_an_edge_changed(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_rename_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        # The paper's new edge is inserted (and the old entity's edge deleted): an edge delta ⇒ refresh.
        assert uow.edges.refreshed == 1

    async def test_refresh_graph_is_not_called_without_an_edge_delta(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_remove_empty_note_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        # An empty note removed, no dirty notes carrying edges, no pre-existing edges deleted ⇒ no delta.
        assert uow.edges.refreshed == 0


# --- a standalone note (no edges) is removed; nothing else changes. No edge is inserted and no
# pre-existing edge is deleted, so the graph refresh is skipped.
_LONE_PATH = "Memory/Entities/orphan.md"
_LONE_ID = "ent.orphan"


def _build_remove_empty_note_deps(uow: FakeKnowledgeBaseUnitOfWork) -> _Deps:
    """Script a pure removal of an edge-free note: nothing on disk, the manifest still has it.

    No inbound linker references it (the closure is empty), it carried no edges, and no other file
    changed — so there are zero edge inserts and zero edge deletes, and the refresh must be skipped.
    """
    reader = FakeKnowledgeBaseReader(units=[])
    embedder = FakeEmbedding()
    uow.manifest.stored = {
        _LONE_PATH: _note_entry(_LONE_PATH, _LONE_ID, content=content_hash(_entity_note(_LONE_ID, "Orphan")), title="Orphan", slug="orphan")
    }
    uow.fingerprint.stored = StoredFingerprint(
        indexer_version="engram-index/1", spec_hash="spec-hash-1", vocabulary_hash="vocabulary-hash-1", embed_model=embedder.model_id
    )
    clean = (CleanPage(id=_LONE_ID, slug="orphan", title="Orphan", aliases=(), path=_LONE_PATH),)
    return _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=clean)


class TestRemovePageStructuralPath:
    async def test_removed_page_id_is_deleted(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_remove_empty_note_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert _LONE_ID in {pid for batch in uow.pages.deleted_ids for pid in batch}


# --- a source mirror is removed: the manifest carried it (a `source` entry keyed by its source_key);
# disk no longer has it. Its source_key must land in the targeted sources delete (cascading raw_chunks).
_MIRROR_PATH = "Sources/8QXFXYMI.md"
_MIRROR_KEY = "8QXFXYMI"
_MIRROR_TEXT = "---\nsource_key: 8QXFXYMI\nprovider: example-library\ntitle: LightRAG\n---\n\n## 1. Intro\n\nBody.\n"


def _source_entry(path: str, source_key: str, *, content: str) -> ManifestEntry:
    """A stored manifest row for a source mirror: page_id NULL, identity hash over its `source_key`."""
    return ManifestEntry(
        path=path,
        kind=FileType.SOURCE,
        content_hash=content,
        identity_hash=identity_hash(stem=source_key, id_="", title="", aliases=()),
        page_id=None,
        source_key=source_key,
    )


def _build_remove_mirror_deps(uow: FakeKnowledgeBaseUnitOfWork) -> _Deps:
    """Script a removed source mirror: the manifest has the mirror, disk has neither it nor its citers."""
    reader = FakeKnowledgeBaseReader(units=[])
    embedder = FakeEmbedding()
    uow.manifest.stored = {_MIRROR_PATH: _source_entry(_MIRROR_PATH, _MIRROR_KEY, content=content_hash(_MIRROR_TEXT))}
    uow.fingerprint.stored = StoredFingerprint(
        indexer_version="engram-index/1", spec_hash="spec-hash-1", vocabulary_hash="vocabulary-hash-1", embed_model=embedder.model_id
    )
    return _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=())


class TestRemoveSourceMirror:
    async def test_removed_mirror_source_key_is_in_the_sources_delete(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_remove_mirror_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert _MIRROR_KEY in {key for batch in uow.sources.deleted_keys for key in batch}


# --- a CHANGED source mirror: the manifest carried it; disk still has it but its body changed. The
# paper that cites it (its value under the spec's declared key field matches the mirror's own
# source_key) is unchanged on disk, but its quotes must re-verify, so the mirror-citer query must
# pull it into the dirty set.
_CITER_PATH = "Memory/Papers/lightrag-2025.md"
_CITER_ID = "paper.lightrag-2025"
_CITER_KEY = "8QXFXYMI"
_MIRROR_TEXT_V1 = "---\nsource_key: 8QXFXYMI\nprovider: example-library\ntitle: LightRAG\n---\n\n## 1. Intro\n\nOriginal body.\n"
_MIRROR_TEXT_V2 = "---\nsource_key: 8QXFXYMI\nprovider: example-library\ntitle: LightRAG\n---\n\n## 1. Intro\n\nRevised body.\n"


def _build_changed_mirror_deps(uow: FakeKnowledgeBaseUnitOfWork) -> _Deps:
    """Script a changed source mirror with one citing paper (unchanged on disk).

    The manifest carries both the paper and the mirror; disk has the paper verbatim and the mirror
    with a new body (a `content_changed` source). No note identity moved, so the only signal that
    pulls the paper into the dirty set is the mirror-citer query: the paper's value under the spec's
    declared key field matches the changed mirror's own key, so it must re-verify its quotes.
    """
    reader = FakeKnowledgeBaseReader(units=[(_CITER_PATH, _paper_note(_PAPER_BODY_V1))], sources=[(_MIRROR_PATH, _MIRROR_TEXT_V2)])
    embedder = FakeEmbedding()
    uow.manifest.stored = {
        _CITER_PATH: _note_entry(
            _CITER_PATH, _CITER_ID, content=content_hash(_paper_note(_PAPER_BODY_V1)), title="LightRAG", slug="lightrag-2025"
        ),
        _MIRROR_PATH: _source_entry(_MIRROR_PATH, _CITER_KEY, content=content_hash(_MIRROR_TEXT_V1)),
    }
    uow.fingerprint.stored = StoredFingerprint(
        indexer_version="engram-index/1", spec_hash="spec-hash-1", vocabulary_hash="vocabulary-hash-1", embed_model=embedder.model_id
    )
    # The clean paper page carries the citing source id (production reads it off kb.pages).
    clean = (CleanPage(id=_CITER_ID, slug="lightrag-2025", title="LightRAG", aliases=(), path=_CITER_PATH, source_id=_CITER_KEY),)
    return _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=clean)


class TestChangedMirrorCiters:
    async def test_citing_page_enters_the_dirty_set(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_changed_mirror_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        # The unchanged paper that cites the changed mirror is re-parsed + re-loaded (quotes re-verify).
        page_ids = {row["id"] for row in uow.pages.upserted_rows}
        assert _CITER_ID in page_ids


class TestEmbedFlag:
    """`run_incremental` honors `IndexKnowledgeBase.embed` (bug 1): `--no-embed` skips widening + embedding."""

    async def test_no_embed_skips_the_embed_stage(self) -> None:
        # A leftover NULL-embedding page is scripted, but --no-embed must skip both the widening
        # pre-pass and the embed stage: the embedder is never called and embed_meta is left untouched.
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(_PAPER_ID)])
        embedder = FakeEmbedding()  # concrete type so the public recorder (`texts`) is assertable
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2, embedder=embedder)

        report = await run_incremental(IndexKnowledgeBase(collection="research", embed=False), deps=deps)

        assert report.embedded == 0
        assert embedder.texts == []  # the embedder was never called
        assert uow.meta.embed_meta is None  # embed_meta is not touched when embedding is skipped

    async def test_embed_runs_the_embed_stage_and_records_meta(self) -> None:
        # With embedding on, the scripted needing-page is embedded (its rebuilt card_text, never "")
        # and the embed-meta contract row is written.
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(_PAPER_ID)])
        embedder = FakeEmbedding()  # concrete type so the public recorder (`texts`) is assertable
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2, embedder=embedder)

        report = await run_incremental(IndexKnowledgeBase(collection="research", embed=True), deps=deps)

        assert report.embedded == 1
        assert embedder.texts != []  # the embedder was called
        assert all(embedder.texts)  # with only non-empty inputs (never "")
        assert uow.meta.embed_meta is not None


class TestOrientationPageSurvivesIncremental:
    """A fileless orientation page must survive an incremental index untouched.

    The synthetic orientation page owns no source file -- an incremental index that touches
    an unrelated file must neither delete it nor prune it away (it is untouched, not re-upserted).
    """

    async def test_a_body_only_edit_leaves_an_existing_about_page_untouched(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)
        uow.pages.existing = {_PAPER_ID, _ENTITY_ID, "meta.about@research"}  # the about page already lives in the DB

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.pages.pruned == []  # the full-rebuild prune never runs on this path
        assert uow.pages.deleted_ids == []  # no targeted delete names the about page either
        assert "meta.about@research" in uow.pages.existing  # still there after the run
        assert "meta.about@research" not in {row["id"] for row in uow.pages.upserted_rows}  # untouched, not re-upserted empty

    async def test_widening_rebuilds_the_scoped_about_page_for_a_non_default_collection(self) -> None:
        """A leftover NULL-embedding row for a scoped orientation id must still widen correctly.

        `meta.about@repo-docs` must be recognised as this collection's own orientation page (not
        dropped as an unmappable owner) and rebuilt with the knowledge base's real orientation text -- the
        collection-scoped counterpart of the widening pre-pass's existing `meta.about` handling.
        """
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow("meta.about@repo-docs")])
        reader = FakeKnowledgeBaseReader(
            units=[(_PAPER_PATH, _paper_note(_PAPER_BODY_V1)), (_ENTITY_PATH, _ENTITY_NOTE)],
            meta={"purpose": "Why the repo-docs knowledge base exists."},
        )
        uow.manifest.stored = {
            _PAPER_PATH: _note_entry(
                _PAPER_PATH, _PAPER_ID, content=content_hash(_paper_note(_PAPER_BODY_V1)), title="LightRAG", slug="lightrag-2025"
            ),
            _ENTITY_PATH: _note_entry(_ENTITY_PATH, _ENTITY_ID, content=content_hash(_ENTITY_NOTE), title="LightRAG", slug="lightrag"),
        }
        embedder = FakeEmbedding()
        uow.fingerprint.stored = StoredFingerprint(
            indexer_version="engram-index/1", spec_hash="spec-hash-1", vocabulary_hash="vocabulary-hash-1", embed_model=embedder.model_id
        )
        deps = _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=(_clean_paper(), _clean_entity()))

        await run_incremental(IndexKnowledgeBase(collection="repo-docs", embed=True), deps=deps)

        about_row = next(row for row in uow.pages.upserted_rows if row["id"] == "meta.about@repo-docs")
        assert "Why the repo-docs knowledge base exists." in str(about_row["card_text"])


# --- two brand-new notes share a filename stem across different directories (e.g. two
# `context.md` files). Both land in `diff.added`, so both are dirty in the same run.
# `_manifest_rows` must resolve each one's manifest entry to its OWN source path.
_COLLISION_A_PATH = "Memory/EngramArea/context.md"
_COLLISION_A_ID = "ent.context-a"
_COLLISION_B_PATH = "Memory/StudioArea/context.md"
_COLLISION_B_ID = "ent.context-b"


def _collision_note(page_id: str, title: str) -> str:
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: entity\n"
        f"title: {title}\n"
        "status: draft\n"
        "entity_type: technique\n"
        "---\n\n"
        "## Definition\n\nA graph-indexed retrieval technique.\n"
    )


def _build_stem_collision_deps(uow: FakeKnowledgeBaseUnitOfWork) -> _Deps:
    """Script two new notes with the same filename stem in different directories.

    The stored manifest carries only the unrelated, unchanged entity note, so both `context.md`
    files land in `diff.added` this run.
    """
    reader = FakeKnowledgeBaseReader(
        units=[
            (_ENTITY_PATH, _ENTITY_NOTE),
            (_COLLISION_A_PATH, _collision_note(_COLLISION_A_ID, "Engram Context")),
            (_COLLISION_B_PATH, _collision_note(_COLLISION_B_ID, "Studio Context")),
        ]
    )
    embedder = FakeEmbedding()
    uow.manifest.stored = {
        _ENTITY_PATH: _note_entry(_ENTITY_PATH, _ENTITY_ID, content=content_hash(_ENTITY_NOTE), title="LightRAG", slug="lightrag")
    }
    uow.fingerprint.stored = StoredFingerprint(
        indexer_version="engram-index/1", spec_hash="spec-hash-1", vocabulary_hash="vocabulary-hash-1", embed_model=embedder.model_id
    )
    return _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=(_clean_entity(),))


class TestManifestStemCollision:
    async def test_each_colliding_page_gets_its_own_manifest_path(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_stem_collision_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research", embed=False), deps=deps)

        upserted = [entry for batch in uow.manifest.manifest_upserts for entry in batch]
        path_by_page_id = {str(entry["page_id"]): str(entry["path"]) for entry in upserted}
        assert path_by_page_id[_COLLISION_A_ID] == _COLLISION_A_PATH
        assert path_by_page_id[_COLLISION_B_ID] == _COLLISION_B_PATH

    async def test_one_manifest_row_per_colliding_file(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_stem_collision_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research", embed=False), deps=deps)

        upserted = [entry for batch in uow.manifest.manifest_upserts for entry in batch]
        paths = [str(entry["path"]) for entry in upserted]
        assert paths.count(_COLLISION_A_PATH) == 1
        assert paths.count(_COLLISION_B_PATH) == 1


class TestUnparseableDirtyNote:
    """A dirty note that fails to parse is a `parse-error` validation row keyed by its path, never an aborted run."""

    _BROKEN_PAPER = _paper_note(_PAPER_BODY_V2).replace("status: synthesized", "status: bogus")

    async def test_the_run_finishes_and_reports_the_parse_error(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2, paper_text=self._BROKEN_PAPER)

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        errors = [issue for issue in report.validation.errors if issue.rule == "parse-error"]
        assert [issue.page_id for issue in errors] == [_PAPER_PATH]
        assert "bogus" in errors[0].message
        changed = next(event for event in uow.coordination.events if isinstance(event, CitingPagesChanged))
        assert changed.page_ids == ()
        assert {row["id"] for row in uow.pages.upserted_rows} == set(), "a note that did not parse is not upserted"

    async def test_the_parse_error_is_stored_under_the_file_path(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2, paper_text=self._BROKEN_PAPER)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        rows = [row for batch in uow.validation_store.validation_rows for row in batch]
        assert [(row["page_id"], row["rule"]) for row in rows] == [(_PAPER_PATH, "parse-error")]
        assert any(_PAPER_PATH in cleared for cleared in uow.validation_store.replaced_page_ids)


class TestStandingValidation:
    """The report carries the issues standing across the collection, not only the dirty pages' own."""

    async def test_an_error_on_an_unchanged_page_stands_in_the_report(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        standing = ValidationIssue(severity="error", rule="dangling-link", page_id=_ENTITY_ID, message="links to a page that does not exist")
        uow.validation_store.report = ValidationReport(errors=(standing,))
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert standing not in report.validation.errors, "the entity did not change, so this run did not validate it"
        assert report.standing_validation == ValidationReport(errors=(standing,))


# --- a `Sources/` mirror naming no `provider`: `mirror_provider_issues` flags it (an error, keyed
# by its own knowledge base path, since it produces no page) and `loadable_mirrors` keeps it out of the
# load. The full path persists that error into `kb.validation_store` unconditionally; this pins the
# incremental path doing the same, not just reporting the error and dropping its storage.
_ORPHAN_MIRROR_PATH = "Sources/orphan.md"
_ORPHAN_MIRROR_KEY = "ORPHAN1"
_ORPHAN_MIRROR_TEXT = "---\nsource_key: ORPHAN1\nsource_hash: aa11\n---\n\n# An Uncredited Mirror\n\nNobody cites this one.\n"


class TestProviderlessMirrorLint:
    """A mirror naming no `provider` is a `mirror-without-provider` validation error, stored like a parse error."""

    async def test_the_run_finishes_and_reports_the_mirror_error(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2, mirrors=[(_ORPHAN_MIRROR_PATH, _ORPHAN_MIRROR_TEXT)])

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        errors = [issue for issue in report.validation.errors if issue.rule == "mirror-without-provider"]
        assert [issue.page_id for issue in errors] == [_ORPHAN_MIRROR_PATH]

    async def test_the_mirror_error_is_stored_under_its_knowledge_base_path(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2, mirrors=[(_ORPHAN_MIRROR_PATH, _ORPHAN_MIRROR_TEXT)])

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        rows = [row for batch in uow.validation_store.validation_rows for row in batch]
        assert any(row["page_id"] == _ORPHAN_MIRROR_PATH and row["rule"] == "mirror-without-provider" for row in rows)
        assert any(_ORPHAN_MIRROR_PATH in cleared for cleared in uow.validation_store.replaced_page_ids)

    async def test_the_mirror_is_not_loaded(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2, mirrors=[(_ORPHAN_MIRROR_PATH, _ORPHAN_MIRROR_TEXT)])

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        loaded_ids = {str(row["source_id"]) for batch in uow.sources.sources for row in batch}
        assert _ORPHAN_MIRROR_KEY not in loaded_ids


def test_a_closure_path_the_vault_no_longer_has_is_skipped_not_fatal() -> None:
    """A vanished dirty path is skipped, never fatal.

    A citer that moved or was deleted since the stored manifest can still be named by the
    closure; it contributes nothing to the reparse instead of crashing the index.
    """
    from engram.indexing.service.incremental import _parse_dirty_notes

    parsed = _parse_dirty_notes({"Wiki/Comparisons/moved-away.md"}, {}, research_spec())
    assert parsed.notes == ()
    assert parsed.errors == ()


# --- the dirty page's stale children: rows a prior index left in the store that the fresh load no
# longer produces. `#tl_dr` is the one section the V2 body still emits, so it must survive.
_STALE_SECTION = f"{_PAPER_ID}#old"
_LIVE_SECTION = f"{_PAPER_ID}#tl_dr"
_STALE_CLAIM = f"{_PAPER_ID}#c2"


def _seed_stale_children(uow: FakeKnowledgeBaseUnitOfWork) -> None:
    """Seed the store with the dirty page's prior children."""
    uow.sections.existing = {_STALE_SECTION, _LIVE_SECTION}
    uow.sections.page_of = {_STALE_SECTION: _PAPER_ID, _LIVE_SECTION: _PAPER_ID}
    uow.claims.existing = {_STALE_CLAIM}
    uow.claims.page_of = {_STALE_CLAIM: _PAPER_ID}
    uow.summaries.section_units = {_STALE_SECTION: _PAPER_ID, _LIVE_SECTION: _PAPER_ID}
    uow.summaries.page_units = {_PAPER_ID}


class _PruneFailedError(RuntimeError):
    """The mid-transaction failure the rollback test injects into the claim reconcile."""


class _ExplodingClaimRepo(_RecordingRepo):
    """A claims repository whose per-page reconcile raises instead of deleting."""

    async def prune_for_pages(self, keep_ids: set[str], page_ids: set[str], *, collection: str) -> int:
        del keep_ids, page_ids, collection
        raise _PruneFailedError


class TestDirtyPageChildReconcile:
    """A dirty page's owned child rows are reconciled to what the fresh load produced, in one transaction."""

    async def test_the_section_the_fresh_load_no_longer_produces_is_deleted(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        _seed_stale_children(uow)
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.sections.existing == {_LIVE_SECTION}
        assert uow.sections.pruned_for_pages == [({_LIVE_SECTION}, {_PAPER_ID})]

    async def test_the_claim_the_fresh_load_no_longer_produces_is_deleted(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        _seed_stale_children(uow)
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.claims.existing == set()
        assert uow.claims.pruned_for_pages == [(set(), {_PAPER_ID})]

    async def test_the_removed_sections_summary_goes_and_the_surviving_units_stay(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        _seed_stale_children(uow)
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.summaries.section_units == {_LIVE_SECTION: _PAPER_ID}
        assert uow.summaries.page_units == {_PAPER_ID}
        assert uow.summaries.pruned_for_pages == [({_LIVE_SECTION}, {_PAPER_ID}, set())]

    async def test_every_reconcile_names_the_collection_being_indexed(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        _seed_stale_children(uow)
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="lab"), deps=deps)

        assert uow.sections.pruned_for_pages_collections == ["lab"]
        assert uow.claims.pruned_for_pages_collections == ["lab"]
        assert uow.summaries.pruned_for_pages_collections == ["lab"]

    async def test_a_dirty_page_that_failed_to_parse_keeps_its_children(self) -> None:
        """A note that did not parse produces no rows, so reconciling against its empty load would wipe it."""
        uow = FakeKnowledgeBaseUnitOfWork()
        _seed_stale_children(uow)
        broken = _paper_note(_PAPER_BODY_V2).replace("status: synthesized", "status: bogus")
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2, paper_text=broken)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.sections.existing == {_STALE_SECTION, _LIVE_SECTION}
        assert uow.claims.existing == {_STALE_CLAIM}
        assert uow.sections.pruned_for_pages == [(set(), set())]

    async def test_the_load_event_carries_the_reconciled_row_count(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        _seed_stale_children(uow)
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        loaded = next(event for event in uow.coordination.events if isinstance(event, PagesLoaded))
        # one section + one claim + one summary
        assert loaded.pruned == 3
        assert report.pruned == 3

    async def test_the_summary_reconcile_runs_before_the_sections_are_deleted(self) -> None:
        """It reads the still-stored old section rows to learn which sections are going away."""
        uow = FakeKnowledgeBaseUnitOfWork()
        _seed_stale_children(uow)
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        summary_prune = uow.calls.index("summaries.prune_for_pages")
        assert summary_prune < uow.calls.index("sections.prune_for_pages")
        assert summary_prune < uow.calls.index("pages.delete_by_ids")

    async def test_the_child_reconciles_sit_between_the_child_upserts_and_the_index_state_writes(self) -> None:
        """`--no-embed` keeps the load transaction the only one this run opens, so its commit is countable."""
        uow = FakeKnowledgeBaseUnitOfWork()
        _seed_stale_children(uow)
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research", embed=False), deps=deps)

        for child in ("sections", "claims"):
            assert uow.calls.count(f"{child}.prune_for_pages") == 1
            assert uow.calls.index(f"{child}.upsert_many") < uow.calls.index(f"{child}.prune_for_pages")
            assert uow.calls.index(f"{child}.prune_for_pages") < uow.calls.index("manifest.upsert_many")
        assert uow.calls.index("manifest.upsert_many") < uow.calls.index("fingerprint.touch") < uow.calls.index("commit")
        assert uow.calls.count("commit") == 1

    async def test_a_failing_child_reconcile_leaves_the_transaction_unwritten(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        _seed_stale_children(uow)
        uow.claims = _ExplodingClaimRepo()
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        with pytest.raises(_PruneFailedError):
            await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.committed is False
        assert uow.rolled_back is True
        assert uow.manifest.manifest_upserts == []
        assert uow.fingerprint.touch_calls == []
        assert uow.edges.refreshed == 0


class TestDeletedPageSummaries:
    """A page deleted this run takes its own and its sections' summaries with it."""

    async def test_the_deleted_pages_summaries_are_named_in_the_reconcile(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.summaries.page_units = {_OLD_ENTITY_ID}
        uow.summaries.section_units = {f"{_OLD_ENTITY_ID}#definition": _OLD_ENTITY_ID}
        deps = _build_rename_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert uow.summaries.page_units == set()
        assert uow.summaries.section_units == {}
        _keep, _dirty, deleted = uow.summaries.pruned_for_pages[0]
        assert deleted == {_OLD_ENTITY_ID}


# --- a retitle: the entity keeps its path and id but its title changes on disk. The paper links the
# entity by its old title and is unchanged on disk, so the closure re-resolves it (its link now dangles).
_OLD_ENTITY_TITLE = "GraphRAG"
_NEW_ENTITY_TITLE = "GraphRAG Index"


def _build_retitle_deps(uow: FakeKnowledgeBaseUnitOfWork) -> _Deps:
    """Script one edited note (the entity's new title) and one linker the closure re-resolves with its file unchanged."""
    reader = FakeKnowledgeBaseReader(
        units=[(_LINKER_PATH, _linker_paper(_OLD_ENTITY_TITLE)), (_OLD_ENTITY_PATH, _entity_note(_OLD_ENTITY_ID, _NEW_ENTITY_TITLE))]
    )
    embedder = FakeEmbedding()
    uow.manifest.stored = {
        _LINKER_PATH: _note_entry(
            _LINKER_PATH, _LINKER_ID, content=content_hash(_linker_paper(_OLD_ENTITY_TITLE)), title="LightRAG", slug="lightrag-2025"
        ),
        _OLD_ENTITY_PATH: _note_entry(
            _OLD_ENTITY_PATH,
            _OLD_ENTITY_ID,
            content=content_hash(_entity_note(_OLD_ENTITY_ID, _OLD_ENTITY_TITLE)),
            title=_OLD_ENTITY_TITLE,
            slug=_OLD_ENTITY_SLUG,
        ),
    }
    uow.fingerprint.stored = StoredFingerprint(
        indexer_version="engram-index/1", spec_hash="spec-hash-1", vocabulary_hash="vocabulary-hash-1", embed_model=embedder.model_id
    )
    # The paper references the title that disappeared, so the name query answers the paper's id.
    uow.link_refs.referencing = {_LINKER_ID}
    clean = (
        CleanPage(id=_LINKER_ID, slug="lightrag-2025", title="LightRAG", aliases=(), path=_LINKER_PATH),
        CleanPage(id=_OLD_ENTITY_ID, slug=_OLD_ENTITY_SLUG, title=_OLD_ENTITY_TITLE, aliases=(), path=_OLD_ENTITY_PATH),
    )
    return _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=clean)


class TestChangedPages:
    """`PagesLoaded.page_ids` lists the notes whose file content changed, never a page loaded again for another reason."""

    async def test_pages_loaded_lists_an_edited_note_and_not_a_relinked_one(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_retitle_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert _LINKER_ID in {row["id"] for row in uow.pages.upserted_rows}, "the closure re-resolves the linker"
        loaded = next(event for event in uow.coordination.events if isinstance(event, PagesLoaded))
        assert loaded.page_ids == (_OLD_ENTITY_ID,)

    async def test_pages_loaded_does_not_list_a_file_the_embedding_backfill_widened_in(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(_ENTITY_ID)])
        deps = _build_deps(uow, paper_body=_PAPER_BODY_V2)

        await run_incremental(IndexKnowledgeBase(collection="research", embed=True), deps=deps)

        assert _ENTITY_ID in {row["id"] for row in uow.pages.upserted_rows}, "the backfill loads the unchanged entity again"
        loaded = next(event for event in uow.coordination.events if isinstance(event, PagesLoaded))
        assert loaded.page_ids == (_PAPER_ID,)
