"""Locks the narrowed `CitingPagesChanged` contract end-to-end.

`CitingPagesChanged.page_ids` is the *changed* digest page set, not the whole knowledge base: an
INCREMENTAL run emits only the dirty digest pages, and a `--full` run or cold start emits all of them.
A digest page is one whose type is a value of the schema's `sources.digest_types` (in the research
spec: paper, talk, article and post). The narrowing is computed in the handler layer from the loaded
pages' types, because the pure `compute_closure` cannot know a page's type.

These specs pin the distinction the per-scenario `test_incremental_handler.py` cannot make (it
has a single paper, so "all papers" and "the dirty paper" coincide) and prove the evals QA-gen
subscriber receives exactly the narrowed set through the real bus:

* a TWO-paper knowledge base with ONE paper edited emits `CitingPagesChanged` carrying ONLY that paper id;
* a non-paper-only edit (an entity rename, no `page_type == "paper"` page dirty) emits no page ids;
* a `--full` run over a two-paper knowledge base emits BOTH paper ids, papers being digest pages;
* the `QaGenerationSubscriber` enqueues exactly the narrowed set on the incremental run, and
  nothing on the empty-set incremental run (the subscriber consumes `page_ids` as-is).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import ImmediateWake, MemoryJobs, MemoryOutbox, MessageBus, Worker
from pydantic import ValidationError

from engram.evals.generation.contracts import QaGenerationSubscriber
from engram.indexing.contracts import (
    DEFAULT_CHUNK_SIZES,
    CitingPagesChanged,
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    KnowledgeBaseReader,
    ManifestEntry,
    content_hash,
    indexer_version,
)
from engram.indexing.domain.manifest import FileType, identity_hash
from engram.indexing.read_models import CleanPage, StoredFingerprint
from engram.indexing.service.incremental import run_incremental
from engram.indexing.service.row_builder import digest_page_ids
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork, FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from core.ai.embedding import BatchEmbedder
    from core.ai.retrieval.contracts import Row

    from engram.indexing.internal_contracts import IncrementalUnitOfWork
    from engram.kb.contracts import KnowledgeBaseSchema

# --- two papers + one entity. Paper A is the one edited (body only); paper B and the entity stay
# clean. Both papers carry a `builds_on` link at the entity so each has a real resolved edge.
_PAPER_A_PATH = "Memory/Papers/lightrag-2025.md"
_PAPER_A_ID = "paper.lightrag-2025"
_PAPER_B_PATH = "Memory/Papers/graphrag-2024.md"
_PAPER_B_ID = "paper.graphrag-2024"
_ENTITY_PATH = "Memory/Entities/graph-rag.md"
_ENTITY_ID = "ent.graph-rag"
_ENTITY_SLUG = "graph-rag"

_PAPER_A_BODY_V1 = "LightRAG indexes a knowledge base as a graph and retrieves at two levels."
_PAPER_A_BODY_V2 = "LightRAG indexes a knowledge base as a graph and retrieves at two levels (revised)."


def _paper(page_id: str, title: str, zotero_key: str, body: str) -> str:
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: paper\n"
        f"title: {title}\n"
        "summary: Graph-indexed RAG.\n"
        "status: synthesized\n"
        f"zotero_key: {zotero_key}\n"
        "year: 2025\n"
        "paper_type: method\n"
        "source_hash: bd72\n"
        f'builds_on: "[[{_ENTITY_SLUG}]]"\n'
        "---\n\n"
        "## TL;DR\n\n"
        f"{body}\n"
    )


def _entity(page_id: str, title: str) -> str:
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


def _note_entry(path: str, page_id: str, *, content: str, title: str, slug: str) -> ManifestEntry:
    return ManifestEntry(
        path=path,
        kind=FileType.NOTE,
        content_hash=content,
        identity_hash=identity_hash(stem=slug, id_=page_id, title=title, aliases=()),
        page_id=page_id,
        source_key=None,
    )


# The fingerprint inputs the real `IndexKnowledgeBaseHandler.handle` computes for a `FakeKnowledgeBaseReader`
# (no vocabulary files → empty `vocabulary_text`), so a handler-driven run takes the INCREMENTAL
# branch (`_can_incremental` matches the stored fingerprint). The `run_incremental`-direct tests
# compare these same values, so one fixture drives both the direct and the bus paths.
_SPEC_HASH = content_hash(research_spec().model_dump_json())
_REGISTRY_HASH = content_hash("")


@dataclass(frozen=True)
class _Deps:
    """A hand-built `IncrementalDeps` for the incremental scenarios (every collaborator a fake)."""

    uow_factory: Callable[[], IncrementalUnitOfWork]
    kb_reader: KnowledgeBaseReader
    embedder: BatchEmbedder
    clean_pages: tuple[CleanPage, ...]
    indexer_version: str = indexer_version(DEFAULT_CHUNK_SIZES)
    spec_hash: str = _SPEC_HASH
    vocabulary_hash: str = _REGISTRY_HASH
    spec: KnowledgeBaseSchema = field(default_factory=research_spec)
    path_root: Path | None = None
    path_root_source: str | None = None

    def clean_page_view(self) -> Sequence[CleanPage]:
        return self.clean_pages


def _matching_fingerprint(embedder: FakeEmbedding) -> StoredFingerprint:
    return StoredFingerprint(
        indexer_version=indexer_version(DEFAULT_CHUNK_SIZES), spec_hash=_SPEC_HASH, vocabulary_hash=_REGISTRY_HASH, embed_model=embedder.model_id
    )


def _build_one_paper_edit_deps(uow: FakeKnowledgeBaseUnitOfWork) -> _Deps:
    """Script a body-only edit of paper A in a knowledge base that ALSO holds a clean paper B + entity.

    The manifest carries all three files; disk has paper A at V2 (a `content_changed` whose
    identity is unchanged) and paper B + the entity verbatim. So the dirty set is exactly paper A:
    if the emission narrowed correctly, `CitingPagesChanged` carries only paper A — NOT paper B.
    """
    reader = FakeKnowledgeBaseReader(
        units=[
            (_PAPER_A_PATH, _paper(_PAPER_A_ID, "LightRAG", "8QXFXYMI", _PAPER_A_BODY_V2)),
            (_PAPER_B_PATH, _paper(_PAPER_B_ID, "GraphRAG", "ABCDEFGH", "GraphRAG builds a knowledge graph then queries it.")),
            (_ENTITY_PATH, _entity(_ENTITY_ID, "GraphRAG")),
        ]
    )
    embedder = FakeEmbedding()
    uow.manifest.stored = {
        _PAPER_A_PATH: _note_entry(
            _PAPER_A_PATH,
            _PAPER_A_ID,
            content=content_hash(_paper(_PAPER_A_ID, "LightRAG", "8QXFXYMI", _PAPER_A_BODY_V1)),
            title="LightRAG",
            slug="lightrag-2025",
        ),
        _PAPER_B_PATH: _note_entry(
            _PAPER_B_PATH,
            _PAPER_B_ID,
            content=content_hash(_paper(_PAPER_B_ID, "GraphRAG", "ABCDEFGH", "GraphRAG builds a knowledge graph then queries it.")),
            title="GraphRAG",
            slug="graphrag-2024",
        ),
        _ENTITY_PATH: _note_entry(
            _ENTITY_PATH, _ENTITY_ID, content=content_hash(_entity(_ENTITY_ID, "GraphRAG")), title="GraphRAG", slug=_ENTITY_SLUG
        ),
    }
    uow.fingerprint.stored = _matching_fingerprint(embedder)
    clean = (
        CleanPage(id=_PAPER_A_ID, slug="lightrag-2025", title="LightRAG", aliases=(), path=_PAPER_A_PATH),
        CleanPage(id=_PAPER_B_ID, slug="graphrag-2024", title="GraphRAG", aliases=(), path=_PAPER_B_PATH),
        CleanPage(id=_ENTITY_ID, slug=_ENTITY_SLUG, title="GraphRAG", aliases=(), path=_ENTITY_PATH),
    )
    return _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=clean)


# --- a non-paper-only edit: the entity file `graph-rag.md` is renamed to `gr.md` (id ent.gr). No
# paper changes on disk; the inbound-closure linker (paper A) re-resolves but is an entity-driven
# dirtying. The dirty set holds an entity (the new one) and re-resolved papers — but the CHANGED
# (content_changed/added) note is the entity. The point: the emitted set must reflect actual dirty
# papers; here paper A IS pulled in (it links the renamed entity), so it is in the set. To assert
# the "no paper dirty" contract we script a pure entity body edit with NO inbound paper linker.
_LONE_ENTITY_PATH = "Memory/Entities/orphan.md"
_LONE_ENTITY_ID = "ent.orphan"
_LONE_ENTITY_SLUG = "orphan"
_LONE_ENTITY_V1 = _entity(_LONE_ENTITY_ID, "Orphan")
_LONE_ENTITY_V2 = (
    "---\n"
    f"id: {_LONE_ENTITY_ID}\n"
    "type: entity\n"
    "title: Orphan\n"
    "status: draft\n"
    "entity_type: technique\n"
    "---\n\n"
    "## Definition\n\nA graph-indexed retrieval technique (revised).\n"
)


def _build_entity_only_edit_deps(uow: FakeKnowledgeBaseUnitOfWork) -> _Deps:
    """Script a body-only edit of a lone entity (no inbound paper linker): zero papers dirty.

    The knowledge base also holds a clean paper, untouched on disk, that does NOT link the entity. So the
    dirty set is exactly the entity, and no `page_type == "paper"` page is loaded — the narrowed
    `CitingPagesChanged` must carry an empty id tuple.
    """
    reader = FakeKnowledgeBaseReader(
        units=[
            (_LONE_ENTITY_PATH, _LONE_ENTITY_V2),
            (_PAPER_B_PATH, _paper(_PAPER_B_ID, "GraphRAG", "ABCDEFGH", "GraphRAG builds a knowledge graph then queries it.")),
        ]
    )
    embedder = FakeEmbedding()
    uow.manifest.stored = {
        _LONE_ENTITY_PATH: _note_entry(
            _LONE_ENTITY_PATH, _LONE_ENTITY_ID, content=content_hash(_LONE_ENTITY_V1), title="Orphan", slug=_LONE_ENTITY_SLUG
        ),
        _PAPER_B_PATH: _note_entry(
            _PAPER_B_PATH,
            _PAPER_B_ID,
            content=content_hash(_paper(_PAPER_B_ID, "GraphRAG", "ABCDEFGH", "GraphRAG builds a knowledge graph then queries it.")),
            title="GraphRAG",
            slug="graphrag-2024",
        ),
    }
    uow.fingerprint.stored = _matching_fingerprint(embedder)
    clean = (
        CleanPage(id=_LONE_ENTITY_ID, slug=_LONE_ENTITY_SLUG, title="Orphan", aliases=(), path=_LONE_ENTITY_PATH),
        CleanPage(id=_PAPER_B_ID, slug="graphrag-2024", title="GraphRAG", aliases=(), path=_PAPER_B_PATH),
    )
    return _Deps(uow_factory=lambda: uow, kb_reader=reader, embedder=embedder, clean_pages=clean)


class TestIncrementalNarrowsToTheChangedPaper:
    async def test_only_the_edited_papers_id_is_recorded(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_one_paper_edit_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        changed = next(event for event in uow.coordination.events if isinstance(event, CitingPagesChanged))
        # Paper B is clean (untouched on disk) so it must NOT appear: the changed set is the dirty set.
        assert changed.page_ids == (_PAPER_A_ID,)
        assert _PAPER_B_ID not in changed.page_ids

    async def test_an_entity_only_edit_records_an_empty_paper_set(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_entity_only_edit_deps(uow)

        await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        changed = next(event for event in uow.coordination.events if isinstance(event, CitingPagesChanged))
        # The only dirty page is the entity; no `page_type == "paper"` page is loaded.
        assert changed.page_ids == ()


# --- a real-bus, real-handler full-rebuild over a two-paper tmp knowledge_base. `--full` (and a cold start)
# is the unchanged path: it emits EVERY paper id, and the QA-gen subscriber enqueues all of them.
_FULL_PAPER_A = """\
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
---

## TL;DR

LightRAG indexes a knowledge base as a graph and retrieves at two levels.
"""

_FULL_PAPER_B = """\
---
id: paper.graphrag-2024
type: paper
title: GraphRAG
summary: Graph-based RAG.
status: synthesized
zotero_key: ABCDEFGH
year: 2024
paper_type: method
source_hash: cd91
---

## TL;DR

GraphRAG builds a knowledge graph then queries it.
"""

_FULL_ENTITY = """\
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


def _two_paper_knowledge_base(tmp_path: Path) -> Path:
    builder = KnowledgeBaseBuilder(tmp_path)
    builder.add_unit_document("Papers", "lightrag-2025", _FULL_PAPER_A)
    builder.add_unit_document("Papers", "graphrag-2024", _FULL_PAPER_B)
    builder.add_unit_document("Entities", "lightrag", _FULL_ENTITY)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    return tmp_path


def _full_handler(knowledge_base_root: Path, *, uow: FakeKnowledgeBaseUnitOfWork) -> IndexKnowledgeBaseHandler:
    ticks = iter([0.0, 1.5, 3.0, 4.5, 6.0])
    return IndexKnowledgeBaseHandler(
        FilesystemKnowledgeBaseReader(knowledge_base_root),
        FakeEmbedding(),
        lambda: uow,
        FakeValidationReportSink(),
        spec=research_spec(),
        clock=lambda: next(ticks),
    )


class TestFullRebuildEmitsAllPapers:
    async def test_full_run_emits_every_paper_id(self, tmp_path: Path) -> None:
        knowledge_base = _two_paper_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _full_handler(knowledge_base, uow=uow)

        await handler.handle(IndexKnowledgeBase(collection="research", full=True))

        changed = next(event for event in uow.coordination.events if isinstance(event, CitingPagesChanged))
        assert set(changed.page_ids) == {_PAPER_A_ID, _PAPER_B_ID}


class _RecordingQueue:
    """A recording `QaGenerationQueue`: each enqueue call is appended for assertions."""

    def __init__(self) -> None:
        self.enqueued: list[list[str]] = []

    async def enqueue(self, page_ids: Sequence[str]) -> None:
        self.enqueued.append(list(page_ids))


def _bus_with_qa_subscriber(handler: IndexKnowledgeBaseHandler, queue: _RecordingQueue) -> MessageBus:
    """Wire the real handler + the real QA-gen subscriber (on `CitingPagesChanged`) onto one bus."""
    bus = MessageBus(command_handlers={IndexKnowledgeBase: handler}, event_subscribers={})
    bus.register_subscriber(CitingPagesChanged, QaGenerationSubscriber(queue, autogen="queue"))
    return bus


def _incremental_handler(uow: FakeKnowledgeBaseUnitOfWork, deps: _Deps) -> IndexKnowledgeBaseHandler:
    """Build the real handler over the concrete `uow` + the deps' fakes, with the clean-page seam set.

    The `uow_factory` is `lambda: uow` (the concrete `FakeKnowledgeBaseUnitOfWork`, which satisfies the full
    `IndexUnitOfWork` the handler needs — not the narrower `IncrementalUnitOfWork` the deps field
    is typed to). The clean-page view is injected via the handler's unit-test seam so the
    full-vs-incremental branch reads the scripted clean pages without a database.
    """
    handler = IndexKnowledgeBaseHandler(deps.kb_reader, deps.embedder, lambda: uow, FakeValidationReportSink(), spec=deps.spec, clock=lambda: 0.0)
    handler.set_clean_page_view(deps.clean_page_view)
    return handler


async def _deliver(bus: MessageBus) -> None:
    """Deliver what the bus put in its in-memory outbox, the way the worker does in production."""
    outbox = bus.outbox
    assert isinstance(outbox, MemoryOutbox)
    await Worker(jobs=MemoryJobs(), outbox=outbox, bus=bus, wake=ImmediateWake(), name="test").deliver_once()


async def _flush(uow: FakeKnowledgeBaseUnitOfWork, bus: MessageBus) -> None:
    """Move what the unit of work recorded into the outbox, the way its commit does in production."""
    await bus.outbox.append(uow.coordination.events)
    uow.coordination.clear()


class TestQaGenSubscriberReceivesTheNarrowedSet:
    async def test_incremental_run_enqueues_only_the_changed_paper(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_one_paper_edit_deps(uow)
        handler = _incremental_handler(uow, deps)
        queue = _RecordingQueue()
        bus = _bus_with_qa_subscriber(handler, queue)

        await bus.handle(IndexKnowledgeBase(collection="research"))
        await _flush(uow, bus)
        await _deliver(bus)

        # The subscriber consumes `CitingPagesChanged.page_ids` as-is: it enqueues exactly the dirty paper.
        assert queue.enqueued == [[_PAPER_A_ID]]

    async def test_entity_only_run_enqueues_nothing(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_entity_only_edit_deps(uow)
        handler = _incremental_handler(uow, deps)
        queue = _RecordingQueue()
        bus = _bus_with_qa_subscriber(handler, queue)

        await bus.handle(IndexKnowledgeBase(collection="research"))
        await _flush(uow, bus)
        await _deliver(bus)

        # An empty `page_ids` is the subscriber's no-op guard: nothing enqueued.
        assert queue.enqueued == []


class TestTheEventField:
    def test_the_event_carries_page_ids(self):
        event = CitingPagesChanged(collection="research", page_ids=("paper.a",))

        assert event.page_ids == ("paper.a",)
        assert "page_ids" in event.model_dump(mode="json")

    def test_a_payload_naming_its_pages_under_another_key_is_refused(self):
        with pytest.raises(ValidationError, match="page_ids"):
            CitingPagesChanged.model_validate({"collection": "research", "paper_ids": ["paper.a"]})


class TestDigestPageIds:
    def test_every_digest_type_queues_and_other_pages_do_not(self):
        rows: list[Row] = [
            {"id": "paper.a", "page_type": "paper"},
            {"id": "talk.b", "page_type": "talk"},
            {"id": "ent.c", "page_type": "entity"},
        ]

        assert digest_page_ids(rows, frozenset({"paper", "talk", "article", "post"})) == ("paper.a", "talk.b")

    def test_a_knowledge_base_with_no_digest_types_queues_nothing(self):
        rows: list[Row] = [{"id": "adr.a", "page_type": "adr"}]

        assert digest_page_ids(rows, frozenset()) == ()
