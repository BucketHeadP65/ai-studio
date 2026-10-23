"""Unit tests for source-mirror removal on the incremental path (`run_incremental`).

A source mirror removed from the vault while an indexed page still cites it must not
break the index: the still-referenced `sources_raw` row's targeted delete is skipped
(deleting it would violate `pages_collection_source_id_fkey`), the citing page — pulled
dirty by the mirror-citer closure — keeps its `source_id` link to the kept row, and the
run reports a `dangling-source-ref` WARNING naming the page and the missing mirror.
Removing the mirror together with its citing page stays a real delete: both targeted
deletes run.

Everything runs over `FakeKnowledgeBaseUnitOfWork` + `FakeKnowledgeBaseReader` (no database, no
filesystem), the `test_incremental_handler` idiom.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engram.indexing.contracts import IndexKnowledgeBase, KnowledgeBaseReader, ManifestEntry, ResolvedNote, content_hash, resolve
from engram.indexing.domain.manifest import DirtySet, FileType, identity_hash
from engram.indexing.read_models import CleanPage, StoredFingerprint
from engram.indexing.service.incremental import _deletable_source_keys, run_incremental
from engram.kb.contracts import FrontmatterFieldSpec, KnowledgeBaseNote, KnowledgeBaseSchema, PageTypeSpec, SourcesSpec, parse_frontmatter
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from core.ai.embedding import BatchEmbedder

    from engram.indexing.internal_contracts import IncrementalUnitOfWork

# A knowledge base of its own whose mirror key field is a knowledge base path, not the research bundle's field name.
_PATH_KEYED_SPEC = KnowledgeBaseSchema(
    slug="handbooks",
    page_types=(
        PageTypeSpec(
            page_type="document",
            id_prefix="doc.",
            frontmatter_fields=(
                FrontmatterFieldSpec(name="source_path", type="scalar"),
                FrontmatterFieldSpec(name="content_hash", type="scalar"),
            ),
        ),
    ),
    sources=SourcesSpec(
        enabled=True,
        connectors=("library",),
        key_field="source_path",
        digest_link_field="derived_from",
        hash_field="content_hash",
        digest_types={"article": "document"},
    ),
)

_PAPER_PATH = "Memory/Papers/dam-2019.md"
_PAPER_ID = "paper.dam-2019"
_SOURCE_PATH = "Sources/dam-2019.md"
_SOURCE_KEY = "arxiv-1907.10340"

_PAPER_NOTE = (
    "---\n"
    f"id: {_PAPER_ID}\n"
    "type: paper\n"
    "title: Dissipative Adiabatic Measurements\n"
    "summary: DAMs read out expectation values without collapsing the steady state.\n"
    "status: synthesized\n"
    f"zotero_key: {_SOURCE_KEY}\n"
    "year: 2019\n"
    "paper_type: method\n"
    "source_hash: ab12\n"
    "---\n\n"
    "## TL;DR\n\nWeak-coupling long-time measurements estimate dissipative parameters.\n"
)

_MIRROR_TEXT = "---\nsource_key: " + _SOURCE_KEY + "\nprovider: example-library\n---\n\n## 1. Intro\n\nMirror text.\n"


@dataclass(frozen=True)
class _Deps:
    """A hand-built `IncrementalDeps` with every collaborator a fake."""

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


def _note_entry() -> ManifestEntry:
    return ManifestEntry(
        path=_PAPER_PATH,
        kind=FileType.NOTE,
        content_hash=content_hash(_PAPER_NOTE),
        identity_hash=identity_hash(stem="dam-2019", id_=_PAPER_ID, title="Dissipative Adiabatic Measurements", aliases=()),
        page_id=_PAPER_ID,
        source_key=None,
    )


def _source_entry() -> ManifestEntry:
    return ManifestEntry(
        path=_SOURCE_PATH,
        kind=FileType.SOURCE,
        content_hash=content_hash(_MIRROR_TEXT),
        identity_hash="src-identity",
        page_id=None,
        source_key=_SOURCE_KEY,
    )


def _build_deps(uow: FakeKnowledgeBaseUnitOfWork, *, paper_on_disk: bool) -> _Deps:
    """Script a vault whose source mirror is gone; `paper_on_disk` keeps or removes its citing page."""
    units = [(_PAPER_PATH, _PAPER_NOTE)] if paper_on_disk else []
    reader = FakeKnowledgeBaseReader(units=units)
    embedder = FakeEmbedding()
    uow.manifest.stored = {_PAPER_PATH: _note_entry(), _SOURCE_PATH: _source_entry()}
    uow.fingerprint.stored = StoredFingerprint(
        indexer_version="engram-index/1", spec_hash="spec-hash-1", vocabulary_hash="vocabulary-hash-1", embed_model=embedder.model_id
    )
    clean = CleanPage(
        id=_PAPER_ID,
        slug="dam-2019",
        title="Dissipative Adiabatic Measurements",
        aliases=(),
        path=_PAPER_PATH,
        source_id=_SOURCE_KEY,
    )
    return _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=(clean,))


class TestRemovedMirrorWithARemainingCiter:
    async def test_the_still_referenced_source_is_not_deleted(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_on_disk=True)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert all(_SOURCE_KEY not in keys for keys in uow.sources.deleted_keys)

    async def test_the_citing_page_keeps_its_source_link(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_on_disk=True)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        row = next(row for row in uow.pages.upserted_rows if row["id"] == _PAPER_ID)
        assert row["source_id"] == _SOURCE_KEY

    async def test_the_run_reports_a_dangling_source_ref_warning(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_on_disk=True)

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        issue = next(i for i in report.validation.warnings if i.rule == "dangling-source-ref")
        assert issue.page_id == _PAPER_ID
        assert _SOURCE_KEY in issue.message

    async def test_the_run_completes_with_no_lint_error_for_the_missing_mirror(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_on_disk=True)

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert report.mode == "incremental"
        assert not any(i.rule == "dangling-source-ref" for i in report.validation.errors)


class TestRemovedMirrorWithItsCiterRemovedToo:
    async def test_both_targeted_deletes_run(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, paper_on_disk=False)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert any(_SOURCE_KEY in keys for keys in uow.sources.deleted_keys)
        assert any(_PAPER_ID in ids for ids in uow.pages.deleted_ids)


class TestTheHeldBackKeySetReadsTheSpecsKeyField:
    """`_deletable_source_keys` asks the surviving notes for their citation under `sources.key_field`.

    Holding a removed mirror's row back depends on recognising that a surviving page still cites it,
    so reading the wrong frontmatter field would delete a row a page's FK still points at.
    """

    def _dirty_set(self) -> DirtySet:
        return DirtySet(
            dirty_paths=frozenset({"Wiki/handbook.md"}),
            deleted_page_ids=frozenset(),
            deleted_source_keys=frozenset({"Sources/handbook.md"}),
        )

    def _note(self) -> ResolvedNote:
        frontmatter = parse_frontmatter(
            {
                "id": "doc.handbook",
                "type": "document",
                "title": "Handbook",
                "status": "draft",
                "source_path": "Sources/handbook.md",
            },
            _PATH_KEYED_SPEC,
        )
        note = KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path="Wiki/handbook.md")
        return resolve((note,)).notes[0]

    def test_a_citation_under_the_declared_field_holds_the_row_back(self) -> None:
        deletable = _deletable_source_keys(self._dirty_set(), clean=(), dirty_notes=(self._note(),), source_key_field="source_path", digest_link_field="")

        assert deletable == set()

    def test_the_same_note_under_another_key_field_holds_nothing_back(self) -> None:
        deletable = _deletable_source_keys(self._dirty_set(), clean=(), dirty_notes=(self._note(),), source_key_field="zotero_key", digest_link_field="")

        assert deletable == {"Sources/handbook.md"}

    def test_a_citation_under_the_digest_link_field_holds_the_row_back(self) -> None:
        frontmatter = parse_frontmatter(
            {"id": "doc.post", "type": "document", "title": "Post", "status": "draft", "derived_from": "Sources/handbook.md"}, _PATH_KEYED_SPEC
        )
        note = resolve((KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path="Wiki/post.md"),)).notes[0]

        deletable = _deletable_source_keys(self._dirty_set(), clean=(), dirty_notes=(note,), source_key_field="source_path", digest_link_field="derived_from")

        assert deletable == set()

    def test_a_knowledge_base_declaring_no_key_field_holds_nothing_back(self) -> None:
        deletable = _deletable_source_keys(self._dirty_set(), clean=(), dirty_notes=(self._note(),), source_key_field="", digest_link_field="")

        assert deletable == {"Sources/handbook.md"}
