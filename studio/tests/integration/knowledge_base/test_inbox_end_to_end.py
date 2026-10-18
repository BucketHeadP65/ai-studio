r"""End-to-end proof for a video-queueing knowledge base (containerized DB, `migrated` fixture).

The full pipeline: a `knowledge-base-schema.json` declaring the `intake` connector is scaffolded (no special case
anywhere downstream) -> a pasted URL lands on its source queue -> `sync` resolves it through the production
`SourceQueueConnector` and writes a real mirror -> `index` (embeddings on, `FakeEmbedding`) chunks it
into `kb.raw_chunks` -> `api.search` returns those chunks at the raw tier, scoped to the `inbox`
collection.

Only the network boundary is faked (`FakeTranscriptFetcher`, the house HTTP-boundary rule). The
connector, its `VideoResolver`, `PassthroughConverter` and `ObsidianMirrorStore` are the production
adapters the `"intake"` connector wires, built here exactly as `SourceQueueConnector.build` builds them.

Two videos exercise the source-queue contract in one pass: both resolve on the first sync, and on the
second both are served from their cache files rather than fetched again, so neither mirror is
rewritten and a second, FRESH `FakeEmbedding` instance's `.texts` after the incremental reindex
prove nothing was re-embedded. A failed row that is retried resolves once more.

A second scenario proves the inbox's other authored page type: a `talk` page citing its synced mirror by
`zotero_key`/`source_hash` indexes clean, and the citation resolves to a mirror whose transcript verifies a
quote drawn straight from it.

Skips when Docker is unavailable (via the `migrated` fixture's `pg` guard).
"""

from __future__ import annotations

import json

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from engram.config import KnowledgeBaseSettings
from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler, MirrorDoc
from engram.indexing.infrastructure import orm
from engram.kb.contracts import CanonicalSection, FrontmatterFieldSpec, KnowledgeBaseSchema, PageTypeSpec, SourcesSpec, quotes_in_mirror
from engram.kb.domain.schema import RetrievalSpec
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.sources.contracts import SourceQueue, SourceQueueStatus, SourceType, SyncMirrors, SyncMirrorsHandler, claimants_for
from engram.sources.infrastructure.mirror import ObsidianMirrorStore
from engram.testing.fakes.embedding import FakeEmbedding
from engram_connectors.conversion.contracts import PassthroughConverter
from engram_connectors.source_queue.contracts import Resolver, SourceQueueConnector, VideoResolver
from engram_connectors.testing.fakes.youtube import FakeTranscriptFetcher
from engram_connectors.youtube.contracts import FetchedVideo, TranscriptSegment, VideoTranscript
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory

if TYPE_CHECKING:
    from pathlib import Path

    from engram.retrieval.domain import SqlResult
    from sqlalchemy.ext.asyncio import AsyncEngine

runner = CliRunner()

_VIDEO_A = "videoAAAA01"
_VIDEO_B = "videoBBBB02"
_KEY_A = f"yt-{_VIDEO_A}"
_KEY_B = f"yt-{_VIDEO_B}"
WATCH_URL_A = f"https://www.youtube.com/watch?v={_VIDEO_A}"
WATCH_URL_B = f"https://www.youtube.com/watch?v={_VIDEO_B}"
_NOW = datetime(2026, 8, 20, tzinfo=UTC)

_DOC_A = VideoTranscript(
    video_id=_VIDEO_A,
    title="Consensus Algorithms Explained",
    channel="Systems Channel",
    published=date(2026, 1, 15),
    segments=(
        TranscriptSegment(start_seconds=0.0, text="This talk explains distributed consensus protocols."),
        TranscriptSegment(start_seconds=5.0, text="Raft simplifies leader election compared to Paxos."),
    ),
)

_DOC_B = VideoTranscript(
    video_id=_VIDEO_B,
    title="Kubernetes Fundamentals",
    channel="Systems Channel",
    published=date(2026, 2, 20),
    segments=(
        TranscriptSegment(start_seconds=0.0, text="Kubernetes orchestrates containers across a cluster."),
        TranscriptSegment(start_seconds=5.0, text="Pods and services form the core building blocks."),
    ),
)


def _inbox_spec() -> KnowledgeBaseSchema:
    """The knowledge base spec this end-to-end test scaffolds: video sources into note/talk pages, collection `inbox`.

    Slug `inbox`, page types `note` (a free-standing note) and `talk` (the authored digest of one
    video, citing its mirror via `zotero_key`/`source_hash`, the same citation fields `research`'s
    `paper` page type carries). Sources are enabled on the `"intake"` connector, which resolves a pasted
    URL through the transcript fetcher faked below; retrieval is scoped to the `inbox` collection.
    """
    return KnowledgeBaseSchema(
        slug="inbox",
        purpose=(
            "A dedicated inbox knowledge_base. Paste a YouTube URL, its transcript becomes a searchable, "
            "citable source here until its final home is known."
        ),
        page_types=(
            PageTypeSpec(page_type="note", id_prefix="note.", canonical_sections=(CanonicalSection(slug="overview"),)),
            PageTypeSpec(
                page_type="talk",
                id_prefix="talk.",
                frontmatter_fields=(
                    FrontmatterFieldSpec(name="zotero_key", type="scalar", required=True),
                    FrontmatterFieldSpec(name="source_hash", type="scalar", required=True),
                ),
                canonical_sections=(CanonicalSection(slug="summary"), CanonicalSection(slug="positions")),
            ),
        ),
        sources=SourcesSpec(
            enabled=True,
            connectors=("intake",),
            key_field="zotero_key",
            digest_link_field="digest_of",
            hash_field="source_hash",
            digest_types={"video": "talk"},
        ),
        retrieval=RetrievalSpec(collection="inbox"),
    )


def _scaffold_inbox(knowledge_base: Path) -> None:
    """Write the inbox spec directly, scaffold through the real CLI, then add the `Units/` tree index needs."""
    (knowledge_base / "knowledge-base-schema.json").write_text(_inbox_spec().model_dump_json(indent=2) + "\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "scaffold", "--knowledge-base", str(knowledge_base)])
    assert result.exit_code == 0, result.output
    (knowledge_base / "Units").mkdir(parents=True, exist_ok=True)


def _add_to_ledger(knowledge_base: Path, url: str) -> None:
    """`studio source-queue add`, the real CLI command (filesystem-only, no container)."""
    result = runner.invoke(app, ["source-queue", "add", url, "--knowledge-base", str(knowledge_base)])
    assert result.exit_code == 0, result.output


def _ledger(knowledge_base: Path) -> SourceQueue:
    """The knowledge base's own source queue, the file `studio source-queue add` writes and the connector reads."""
    return SourceQueue(knowledge_base / "_meta" / "source-queue.yaml", connectors=claimants_for(("youtube", "intake")))


def _sync_handler(knowledge_base: Path, fetcher: FakeTranscriptFetcher) -> SyncMirrorsHandler:
    """The real sync pipeline over `knowledge_base`, wired as the `"intake"` connector entry wires it.

    Only the transcript fetcher (the network boundary) is faked: the connector, its resolver, the
    converter and the mirror store are all production.
    """
    cache_dir = knowledge_base / "_meta" / "source-queue-cache"
    resolvers: dict[SourceType, Resolver] = {SourceType.VIDEO: VideoResolver(fetcher)}
    connector = SourceQueueConnector(_ledger(knowledge_base), resolvers, cache_dir, clock=lambda: _NOW)
    store = ObsidianMirrorStore(KnowledgeBaseSettings(path=knowledge_base))
    return SyncMirrorsHandler(connector, PassthroughConverter("intake"), store, clock=lambda: _NOW)


async def _index(engine: AsyncEngine, knowledge_base: Path, embedding: FakeEmbedding, *, spec: KnowledgeBaseSchema) -> None:
    """Drive the real `IndexKnowledgeBaseHandler` (real PG, the given `FakeEmbedding`) into the `inbox` collection.

    `spec` governs page-type/kind recognition (`IndexKnowledgeBaseHandler` defaults to the bundled research spec when
    omitted). Pass the knowledge base's own scaffolded `knowledge-base-schema.json` when authored notes use inbox-only kinds (`talk`).
    """
    handler = IndexKnowledgeBaseHandler(
        FilesystemKnowledgeBaseReader(knowledge_base), embedding, KnowledgeBaseUnitOfWorkFactory(engine), FileValidationReportSink(knowledge_base), spec=spec
    )
    report = await handler.handle(IndexKnowledgeBase(collection="inbox"))
    assert report.validation.n_errors == 0, report.validation.errors


_SEARCH_SQL = (
    "SELECT * FROM api.search("
    "query => :q, k => :k, query_vec => CAST(:qvec AS text)::halfvec, "
    "facets => CAST(:facets AS text)::jsonb, level => :level, only_current => :only_current, collection => :collection)"
)


async def _search(reader_url: str, query: str, *, level: str | None = "raw", collection: str = "inbox") -> SqlResult:
    """Run one `api.search` call (BM25-only, `query_vec` NULL) through the read-only gateway."""
    params = {"q": query, "k": 10, "qvec": None, "facets": json.dumps({}), "level": level, "only_current": True, "collection": collection}
    engine = create_async_engine(reader_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        return await ReadOnlySqlGateway(engine).rows(_SEARCH_SQL, params, cap=50)
    finally:
        await engine.dispose()


def _snippets(result: SqlResult) -> list[str]:
    return [str(row["snippet"]) for row in result.rows]


class TestInboxEndToEnd:
    """Scaffold -> ledger add -> sync -> index -> search, plus the queue connector's cache contract."""

    async def test_full_pipeline(self, migrated: str, reader_url: str, tmp_path: Path) -> None:
        knowledge_base = tmp_path / "inbox"
        knowledge_base.mkdir()
        _scaffold_inbox(knowledge_base)
        _add_to_ledger(knowledge_base, WATCH_URL_A)
        _add_to_ledger(knowledge_base, WATCH_URL_B)
        inbox_spec = KnowledgeBaseSchema.from_path(knowledge_base / "knowledge-base-schema.json")

        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)

        fetcher = FakeTranscriptFetcher(
            videos={_VIDEO_A: FetchedVideo(doc=_DOC_A, url=WATCH_URL_A), _VIDEO_B: FetchedVideo(doc=_DOC_B, url=WATCH_URL_B)}
        )
        handler = _sync_handler(knowledge_base, fetcher)

        # -- initial sync: both requested rows resolve and become new mirrors --
        first_sync = await handler.handle(SyncMirrors())
        assert first_sync.created == 2
        assert first_sync.updated == 0
        assert first_sync.skipped == 0
        assert first_sync.errors == ()
        assert sorted(fetcher.fetched) == [_VIDEO_A, _VIDEO_B]
        assert {row.key: row.status for row in _ledger(knowledge_base).entries()} == {
            _KEY_A: SourceQueueStatus.RESOLVED,
            _KEY_B: SourceQueueStatus.RESOLVED,
        }

        # -- initial index: embeddings on, FakeEmbedding, the knowledge base's own scaffolded spec --
        first_embedding = FakeEmbedding()
        await _index(engine, knowledge_base, first_embedding, spec=inbox_spec)

        # -- api.search returns transcript chunks at the raw tier, scoped to inbox --
        paxos_hits = await _search(reader_url, "Raft leader election Paxos consensus")
        assert paxos_hits.rows, "expected a raw-chunk hit for video A's transcript"
        assert {row["hit_level"] for row in paxos_hits.rows} == {"raw"}
        assert any("Paxos" in snippet for snippet in _snippets(paxos_hits))

        kubernetes_hits = await _search(reader_url, "Kubernetes containers cluster pods")
        assert kubernetes_hits.rows, "expected a raw-chunk hit for video B's transcript"
        assert any("Kubernetes" in snippet for snippet in _snippets(kubernetes_hits))

        # -- the source-queue contract: a resolved row is served from its cache, never fetched again --
        second_sync = await handler.handle(SyncMirrors())
        assert second_sync.created == 0
        assert second_sync.updated == 0
        assert second_sync.skipped == 2
        assert second_sync.errors == ()
        assert sorted(fetcher.fetched) == [_VIDEO_A, _VIDEO_B], "the second pass reads the cache, it does not refetch"

        # -- incremental reindex with a FRESH embedder: no mirror moved, so nothing is re-embedded --
        second_embedding = FakeEmbedding()
        await _index(engine, knowledge_base, second_embedding, spec=inbox_spec)

        embedded_text = " ".join(second_embedding.texts)
        assert "Paxos" not in embedded_text
        assert "Kubernetes" not in embedded_text

        still_there = await _search(reader_url, "Raft leader election Paxos consensus")
        assert still_there.rows
        assert any("Paxos" in snippet for snippet in _snippets(still_there))

        await engine.dispose()

    async def test_a_failed_row_that_is_retried_resolves_again(self, migrated: str, tmp_path: Path) -> None:
        """The other half of the source-queue contract: `failed` withholds the row, `studio source-queue retry` gives it back."""
        del migrated
        knowledge_base = tmp_path / "inbox"
        knowledge_base.mkdir()
        _scaffold_inbox(knowledge_base)
        _add_to_ledger(knowledge_base, WATCH_URL_A)
        _add_to_ledger(knowledge_base, WATCH_URL_B)

        fetcher = FakeTranscriptFetcher(
            videos={_VIDEO_A: FetchedVideo(doc=_DOC_A, url=WATCH_URL_A), _VIDEO_B: FetchedVideo(doc=_DOC_B, url=WATCH_URL_B)}
        )
        handler = _sync_handler(knowledge_base, fetcher)
        assert (await handler.handle(SyncMirrors())).created == 2

        _ledger(knowledge_base).mark(_KEY_B, SourceQueueStatus.FAILED, reason="captions withdrawn")
        without_b = await handler.handle(SyncMirrors())
        assert without_b.archived == 1, "a failed row contributes no item, so its mirror is archived"

        retried = runner.invoke(app, ["source-queue", "retry", _KEY_B, "--knowledge-base", str(knowledge_base)])
        assert retried.exit_code == 0, retried.output
        after_retry = await handler.handle(SyncMirrors())

        assert (after_retry.created, after_retry.errors) == (1, ())
        row = _ledger(knowledge_base).get(_KEY_B)
        assert row is not None
        assert row.status is SourceQueueStatus.RESOLVED

    async def test_talk_page_cites_mirror_and_quote_verifies(self, migrated: str, tmp_path: Path) -> None:
        """A `talk` page's `zotero_key`/`source_hash` citation indexes clean and resolves to its synced mirror.

        The talk page's `zotero_key` finds the mirror `MirrorDoc.from_text` parses from `Sources/`, and a real
        quote drawn from that video's transcript verifies against the mirror's text.
        """
        knowledge_base = tmp_path / "inbox"
        knowledge_base.mkdir()
        _scaffold_inbox(knowledge_base)
        _add_to_ledger(knowledge_base, WATCH_URL_A)

        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)

        fetcher = FakeTranscriptFetcher(videos={_VIDEO_A: FetchedVideo(doc=_DOC_A, url=WATCH_URL_A)})
        sync_report = await _sync_handler(knowledge_base, fetcher).handle(SyncMirrors())
        assert sync_report.created == 1
        assert sync_report.errors == ()

        # The mirror's own `source_hash`, read back through the real store -- the citation contract a talk page
        # authors against (`zotero_key` = the queued row's key, `source_hash` = the mirror's, same fields
        # `research`'s `paper` page type carries).
        mirror = (await ObsidianMirrorStore(KnowledgeBaseSettings(path=knowledge_base)).load_index())[_KEY_A]

        talk_note = f"""\
---
id: talk.{_VIDEO_A}
type: talk
title: Consensus algorithms, a viewing digest
status: draft
zotero_key: {_KEY_A}
source_hash: {mirror.source_hash}
---

## Summary

A talk on distributed consensus, contrasting Raft against Paxos.

## Positions

Raft is presented as the more approachable protocol for production systems.
"""
        (knowledge_base / "Units" / f"talk.{_VIDEO_A}.md").write_text(talk_note, encoding="utf-8")

        # The scaffolded inbox spec (not the bundled research default `_index` otherwise falls back to) --
        # only it declares the `talk` kind the note above authors against.
        inbox_spec = KnowledgeBaseSchema.from_path(knowledge_base / "knowledge-base-schema.json")
        await _index(engine, knowledge_base, FakeEmbedding(), spec=inbox_spec)  # asserts report.validation.n_errors == 0

        # The quote check, driven exactly as the indexer's validate stage drives it: every
        # `Sources/` mirror parsed to a `MirrorDoc`, keyed by the mirror's own `source_key` -- looked up here
        # by the talk page's own citation field, proving the citation "finds" its mirror before the quote is
        # checked against it.
        reader = FilesystemKnowledgeBaseReader(knowledge_base)
        mirror_docs = (MirrorDoc.from_text(raw.path, raw.text, "inbox") for raw in reader.source_mirrors())
        mirror_texts = {doc.source_key: doc.text for doc in mirror_docs}
        cited_text = mirror_texts.get(_KEY_A)
        assert cited_text is not None, "the talk page's cited key should resolve to its synced mirror"
        assert quotes_in_mirror("Raft simplifies leader election compared to Paxos.", cited_text)

        await engine.dispose()
