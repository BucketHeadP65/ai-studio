r"""End-to-end index (+ later search) over a real ParadeDB — Docker-gated.

This exercises the whole write path through the public CLI: a mini `knowledge_base_builder`
knowledge base → `alembic upgrade head` (the `migrated` fixture) → `memory index` with the
container overridden so **only** the embedder is a :class:`FakeEmbedding` (the
knowledge base reader, the real :class:`SqlAlchemyKnowledgeBaseUnitOfWork` over the throwaway PG, and the
dashboard writer are all production). It asserts the memory rows landed and that a second
identical index re-embeds nothing — hash-gating proven end to end.

The whole module skips when Docker is unavailable (the `pg` fixture, via `migrated`,
raises `pytest.skip`), so `uv run pytest` stays green on a Docker-less machine.

The search half runs over the *same* indexed knowledge base: `memory search` (both the
hybrid path — the embedder overridden to :class:`FakeEmbedding`, the same vector function the
index half seeded with, so the query ranks near its document — and a `--bm25-only` path
that touches no embedder) finds the seeded claim's page; `memory fetch <id>` then returns it.
The search half reuses the production :class:`~engram.retrieval.infrastructure.gateway.ReadOnlySqlGateway` over
the read-only `kb_reader` role (the `reader_url` fixture from `tests/conftest.py`).
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from core.ai.embedding import BatchEmbedder
from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler, KnowledgeBaseReader
from engram.indexing.infrastructure import orm
from engram.kb.contracts import load_knowledge_base_schema
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.settings import RetrievalSettings
from studio.knowledge_base.wiring.contracts import AdminSqlGateway, ReaderSqlGateway, SearchReranking
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

runner = CliRunner()

# A minimal but well-formed paper + the entity its claim links to + a matching mirror,
# so a full index produces pages, sections, and at least one claim with no validation errors.
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

Preamble-only retrieval phrase.

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


def _build_knowledge_base(root: Path) -> Path:
    """Lay down the mini knowledge base (paper + entity + mirror + registries + meta stubs)."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Wiki/Papers", "lightrag-2025", _PAPER)
    builder.add_unit_document("Wiki/Entities", "lightrag", _ENTITY)
    builder.add_source_mirror("lightrag-2025", _MIRROR)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


class _Factory:
    """A `ctx.obj` container factory whose `MessageBus` runs a real-PG, fake-embed handler."""

    def __init__(self, engine: AsyncEngine, knowledge_base: Path) -> None:
        self._reader = FilesystemKnowledgeBaseReader(knowledge_base, note_folders=load_knowledge_base_schema(knowledge_base).note_folders)
        handler = IndexKnowledgeBaseHandler(
            self._reader,
            FakeEmbedding(),  # the ONLY fake — real reader + real PG UoW + real dashboard writer
            KnowledgeBaseUnitOfWorkFactory(engine),
            FileValidationReportSink(knowledge_base),
            spec=load_knowledge_base_schema(knowledge_base),
        )
        self._bus = MessageBus({IndexKnowledgeBase: handler}, {})
        self._gateway = AdminSqlGateway(engine)  # the identity announce's own read, over the same admin engine

    def __call__(self) -> _Container:
        return _Container(self._bus, self._gateway, self._reader)


class _Container:
    """Minimal async container exposing the message bus and identity-announce reads to the CLI command."""

    def __init__(self, bus: MessageBus, gateway: AdminSqlGateway, reader: KnowledgeBaseReader) -> None:
        self._bus = bus
        self._gateway = gateway
        self._reader = reader
        self._jobs = MemoryJobs()

    async def get(self, key: type) -> object:
        if key is MessageBus:
            return self._bus
        if key is AdminSqlGateway:
            return self._gateway
        if key is JobQueries:
            return self._jobs
        if key is KnowledgeBaseReader:
            return self._reader
        msg = f"no registration for {key!r}"
        raise KeyError(msg)


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    """An engine over the freshly-migrated throwaway PG with the pgvector codec registered."""
    eng = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(eng)
    try:
        yield eng
    finally:
        await eng.dispose()


# The knowledge base's Postgres schema (engram migration 0046 renamed `memory` to `kb`). No public
# constant exports this name (`engram.db.contracts.KbBase.metadata.schema` is unset; `engram.indexing.infrastructure.orm`
# keeps its own `_SCHEMA` private), so it is named once here.
_KB_SCHEMA = "kb"


def _count(engine: AsyncEngine, table: str) -> int:
    """Row count of `<_KB_SCHEMA>.<table>` (a fixed allowlist of indexed tables).

    The `TestIndex` methods are *synchronous* — they invoke the Typer CLI, whose
    `index` command calls `asyncio.run` internally, so the test body must have no
    running event loop. This readback therefore wraps its own `asyncio.run` (legal
    here precisely because the sync test body has no loop running).
    """
    return _run(_count_async(engine, table))


async def _count_async(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        return await conn.scalar(text(f"SELECT count(*) FROM {_KB_SCHEMA}.{table}")) or 0  # noqa: S608 — fixed table name


def _scalar(engine: AsyncEngine, sql: str) -> object:
    """Run a single-value read (one-column, one-row) from a sync test body via `asyncio.run`."""
    return _run(_scalar_async(engine, sql))


async def _scalar_async(engine: AsyncEngine, sql: str) -> object:
    async with engine.connect() as conn:
        return await conn.scalar(text(sql))


def _run[T](coro: Coroutine[object, object, T]) -> T:
    """Drive a coroutine to completion from a synchronous test body (no loop is running)."""
    return asyncio.run(coro)


class TestIndex:
    # These tests are *synchronous*: `runner.invoke` drives the Typer `index`
    # command, whose handler calls `asyncio.run` — so the test body must not run
    # inside an event loop (an `async def` test would, raising "asyncio.run() cannot be
    # called from a running event loop"). DB readbacks go through the `_count` / `_scalar`
    # helpers, each wrapping its own `asyncio.run` (legal here — no loop is running). The
    # async `engine` / `migrated` fixtures still resolve for a sync test under
    # `asyncio_mode=auto` (pytest-asyncio runs them in their own loop).
    def test_index_loads_pages_sections_and_claims(self, engine: AsyncEngine, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)

        result = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))

        assert result.exit_code == 0, result.stdout
        assert _count(engine, "pages") == 3  # paper + entity + the meta.about content page
        assert _count(engine, "sections") >= 2
        assert _count(engine, "claims") == 1

    def test_index_writes_embed_meta_and_vectors(self, engine: AsyncEngine, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)

        result = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))

        assert result.exit_code == 0, result.stdout
        model = _scalar(engine, "SELECT model FROM kb.embed_meta LIMIT 1")
        embedded_pages = _scalar(engine, "SELECT count(*) FROM kb.pages WHERE embedding IS NOT NULL")
        assert model == "fake-embed"
        assert embedded_pages == 3  # all three pages were vectorised on the first index

    def test_reindex_is_idempotent_and_re_embeds_nothing(self, engine: AsyncEngine, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))
        edges_after_first = _count(engine, "edges")

        # Second identical index: same knowledge base, unchanged hashes → nothing needs embedding.
        second = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))

        assert second.exit_code == 0, second.stdout
        assert "Embedded: 0" in second.stdout
        # No id vanished from the knowledge base, so the prune DELETEs remove nothing: the true prune
        # count is 0, not the page count (the report counts rows actually deleted).
        assert "Pruned: 0" in second.stdout
        assert _count(engine, "pages") == 3  # idempotent: no duplicate rows
        # Edge count is stable too: re-indexing must not duplicate edges. Claim/section edges carry
        # `src_id` `<page_id>#<anchor>`, not a `kb.pages` id, so the edge wipe must scope by page
        # prefix — an `src_id IN (pages)` wipe misses them and accumulates a duplicate per reindex.
        assert _count(engine, "edges") == edges_after_first


def _embedded_count(stdout: str) -> int:
    """Parse the `Embedded: N` count out of an index report's stdout.

    The report prints `Pruned: P | Embedded: E (T tokens), reused: R`, the tokens only when some were spent
    (`studio.knowledge_base.cli_support._print_index_report`).
    Pulling `E` back out lets the incremental run assert it re-embedded *fewer* units than the
    cold full rebuild — the e2e signal that untouched rows were skipped, not re-vectorised.
    """
    marker = "Embedded: "
    tail = stdout.split(marker, 1)[1]
    return int(tail.split(maxsplit=1)[0].rstrip(","))


# An edited paper body carrying a distinctive phrase the incremental index must surface in
# search. Same frontmatter (so the resolution keys, hence the inbound closure, are unchanged —
# this is a body-only edit): only the TL;DR sentence changes, so only the paper re-embeds.
_PAPER_EDITED = _PAPER.replace(
    "LightRAG indexes a knowledge base as a graph and retrieves at two levels.",
    "LightRAG indexes a knowledge base as a graph and retrieves at two levels using HYDROPLANE traversal.",
)


class TestIncrementalIndex:
    """The incremental edit→search path + the `--full` parity check.

    Drives the SAME real-PG, fake-embed write path as `TestIndex`: a baseline **full** index,
    a one-note body edit, then a bare `studio index` (incremental by default). It
    asserts (a) `studio search` reflects the edit through the read-only `kb_reader` role, and (b)
    the untouched pages were NOT re-embedded (the incremental run re-embeds fewer units than the cold
    full rebuild, and the untouched entity keeps its embedding). Finally a `index --full` run
    immediately after must reproduce an identical DB (row counts per table + the edited card spot-row),
    the e2e shadow of the `test_incremental_equals_full.py` oracle.

    Synchronous like `TestIndex` (the CLI's `asyncio.run` forbids a running loop); DB readbacks go
    through the `_count` / `_scalar` helpers, each wrapping its own `asyncio.run`.
    """

    def test_incremental_index_reflects_the_edit_in_search(self, engine: AsyncEngine, reader_url: str, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        assert runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base)).exit_code == 0

        # Edit one note's body in place, then a bare `index` (incremental by default).
        (knowledge_base / "Wiki" / "Papers" / "lightrag-2025.md").write_text(_PAPER_EDITED, encoding="utf-8")
        incremental = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))

        assert incremental.exit_code == 0, incremental.stdout
        # The edited phrase is now in the served `card_text`: BM25-only search (no embedder ranking
        # needed) surfaces the page through the read-only reader role.
        stdout = _stdout_of(_ServeFactory(engine, reader_url, knowledge_base), ["search", "HYDROPLANE traversal", "--bm25-only", "--knowledge-base", str(knowledge_base)])
        assert "paper.lightrag-2025" in stdout

    def test_incremental_index_does_not_re_embed_untouched_rows(self, engine: AsyncEngine, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        first = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))
        assert first.exit_code == 0, first.stdout
        full_embedded = _embedded_count(first.stdout)

        (knowledge_base / "Wiki" / "Papers" / "lightrag-2025.md").write_text(_PAPER_EDITED, encoding="utf-8")
        incremental = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))

        assert incremental.exit_code == 0, incremental.stdout
        incremental_embedded = _embedded_count(incremental.stdout)
        # A body-only edit re-embeds the changed paper's units but NOT the untouched entity / meta
        # pages: strictly fewer than the cold rebuild, and strictly more than zero (the paper did
        # change). The untouched entity page keeps a non-NULL embedding — its row was never touched.
        assert 0 < incremental_embedded < full_embedded
        entity_embedded = _scalar(engine, "SELECT count(*) FROM kb.pages WHERE id = 'ent.lightrag' AND embedding IS NOT NULL")
        assert entity_embedded == 1
        assert _count(engine, "pages") == 3  # incremental load did not drop / duplicate rows

    def test_full_index_after_incremental_reproduces_an_identical_db(self, engine: AsyncEngine, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path)
        assert runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base)).exit_code == 0
        (knowledge_base / "Wiki" / "Papers" / "lightrag-2025.md").write_text(_PAPER_EDITED, encoding="utf-8")
        assert runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base)).exit_code == 0

        counts_after_incremental = {table: _count(engine, table) for table in _PARITY_TABLES}
        edited_section_after_incremental = _scalar(engine, _EDITED_SECTION_SQL)
        # The incremental load really did rewrite the edited TL;DR section's served body.
        assert "HYDROPLANE traversal" in str(edited_section_after_incremental)

        # A `--full` rebuild immediately after the incremental run must reproduce the same DB
        # (the e2e shadow of the bit-for-bit `test_incremental_equals_full.py` oracle): identical
        # per-table row counts and the same edited section body the incremental path produced.
        full = runner.invoke(app, ["index", "--full", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))

        assert full.exit_code == 0, full.stdout
        assert {table: _count(engine, table) for table in _PARITY_TABLES} == counts_after_incremental
        assert _scalar(engine, _EDITED_SECTION_SQL) == edited_section_after_incremental


# The serving-facing `memory.*` data tables compared for incremental-vs-full row-count parity (the
# e2e shadow of `test_incremental_equals_full.py::_DATA_TABLES`; a count + spot-row check keeps the
# e2e light, the integration oracle owns the full byte-equality dump).
_PARITY_TABLES: tuple[str, ...] = ("pages", "sections", "claims", "edges", "sources_raw", "raw_chunks", "vocab")

# The paper's TL;DR section body carries the edited prose (`_PAPER_EDITED`): the page `card_text` is
# a compact summary line, so the reworded sentence lands in `kb.sections.body`, not the card. This
# is the spot-row both the incremental and the full rebuild must agree on.
_EDITED_SECTION_SQL = "SELECT body FROM kb.sections WHERE page_id = 'paper.lightrag-2025' AND heading = 'TL;DR'"


class _ServeFactory:
    """A `ctx.obj` factory whose container serves the read side over the **reader** role.

    Mirrors the production serve wiring with the one e2e seam: the embedder is a
    :class:`FakeEmbedding` (the same hash-seeded vector function the index half embedded
    pages with, so the query vector ranks near its document), while the gateway is the
    production :class:`ReadOnlySqlGateway` over the ``kb_reader`` engine. ``--bm25-only``
    never asks for :class:`BatchEmbedder`, so a BM25-only search runs with no embedder at all.

    The engine is created eagerly (the pgvector codec registered on it so the `halfvec`
    query vector round-trips) and disposed by the test once the invocation returns.
    """

    def __init__(self, admin_engine: AsyncEngine, reader_url: str, knowledge_base: Path) -> None:
        self.engine = create_async_engine(reader_url, poolclass=NullPool)
        _run(orm.register_pgvector(self.engine))
        self._gateway = ReadOnlySqlGateway(self.engine)
        self._embedder = FakeEmbedding()
        self._admin_gateway = AdminSqlGateway(admin_engine)  # the identity announce's read, over the same migrated DB
        self._reader = FilesystemKnowledgeBaseReader(knowledge_base, note_folders=load_knowledge_base_schema(knowledge_base).note_folders)

    def __call__(self) -> _ServeContainer:
        return _ServeContainer(self._gateway, self._embedder, self._admin_gateway, self._reader)

    def dispose(self) -> None:
        _run(self.engine.dispose())


class _ServeContainer:
    """Resolves the serve read-side deps the search/fetch commands ask for (over the reader).

    `search` resolves the reader gateway + (only on the hybrid path) the embedder and builds
    its handler; `fetch` builds its own `FetchHandler` fresh over the same reader gateway (never a
    DI singleton, so `--collection` can vary per invocation). On ``--bm25-only`` the CLI never asks
    for :class:`BatchEmbedder`, so no embedder is constructed. The identity announce every command
    opens with resolves the admin gateway and job store on top.
    """

    def __init__(
        self,
        gateway: ReadOnlySqlGateway,
        embedder: FakeEmbedding,
        admin_gateway: AdminSqlGateway,
        reader: KnowledgeBaseReader,
    ) -> None:
        self._gateway = gateway
        self._reader = reader
        self._embedder = embedder
        self._admin_gateway = admin_gateway
        self._jobs = MemoryJobs()

    async def get(self, key: type) -> object:
        if key is ReaderSqlGateway:
            return self._gateway
        if key is BatchEmbedder:
            return self._embedder
        if key is AdminSqlGateway:
            return self._admin_gateway
        if key is JobQueries:
            return self._jobs
        if key is KnowledgeBaseReader:
            return self._reader
        if key is RetrievalSettings:
            return RetrievalSettings()
        if key is SearchReranking:
            return SearchReranking(reranking=None)
        msg = f"no registration for {key!r}"
        raise KeyError(msg)


@pytest.fixture
def seeded_knowledge_base(tmp_path: Path) -> Path:
    """The mini knowledge base on disk, shared by the index seeding and the commands that name it with `--knowledge-base`."""
    return _build_knowledge_base(tmp_path)


@pytest.fixture
def seeded_reader(engine: AsyncEngine, reader_url: str, seeded_knowledge_base: Path) -> str:
    """Index the mini knowledge base into the migrated DB, then hand back the read-only reader URL.

    `engine` + `reader_url` are the *same* freshly-migrated database (both ride the
    function-scoped `migrated` fixture): we seed it through the real `memory index` (fake
    embedder, real PG) and return the `kb_reader` URL the serve layer reads through.
    """
    result = runner.invoke(app, ["index", "--knowledge-base", str(seeded_knowledge_base)], obj=_Factory(engine, seeded_knowledge_base))
    assert result.exit_code == 0, result.stdout
    return reader_url


def _stdout_of(factory: _ServeFactory, args: list[str]) -> str:
    """Invoke the CLI with `factory` as the seam, dispose its engine, return stdout."""
    try:
        result = runner.invoke(app, args, obj=factory)
    finally:
        factory.dispose()
    assert result.exit_code == 0, result.stdout
    return result.stdout


class TestSearch:
    """The search half: `memory search` finds the seeded claim, `memory fetch` reads it.

    Runs over the SAME indexed knowledge base as `TestIndex` (via `seeded_reader`), proving the
    end-to-end retrieval path through the public CLI + the read-only `kb_reader` role: the
    hybrid path (FakeEmbedding query vector) and the BM25-only path both surface the seeded
    page, and `fetch` returns the unit's text by id.
    """

    def test_bm25_only_search_finds_the_indexed_page(self, engine: AsyncEngine, seeded_reader: str, seeded_knowledge_base: Path):
        stdout = _stdout_of(
            _ServeFactory(engine, seeded_reader, seeded_knowledge_base), ["search", "LightRAG outperforms GraphRAG", "--bm25-only", "--knowledge-base", str(seeded_knowledge_base)]
        )

        assert "paper.lightrag-2025" in stdout

    def test_hybrid_search_finds_the_indexed_page(self, engine: AsyncEngine, seeded_reader: str, seeded_knowledge_base: Path):
        stdout = _stdout_of(
            _ServeFactory(engine, seeded_reader, seeded_knowledge_base), ["search", "LightRAG outperforms GraphRAG", "--knowledge-base", str(seeded_knowledge_base)]
        )

        assert "paper.lightrag-2025" in stdout

    def test_search_and_fetch_reach_the_indexed_preamble(self, engine: AsyncEngine, seeded_reader: str, seeded_knowledge_base: Path):
        search_stdout = _stdout_of(
            _ServeFactory(engine, seeded_reader, seeded_knowledge_base),
            ["search", "Preamble-only retrieval phrase", "--bm25-only", "--knowledge-base", str(seeded_knowledge_base)],
        )
        fetch_stdout = _stdout_of(
            _ServeFactory(engine, seeded_reader, seeded_knowledge_base),
            ["fetch", "paper.lightrag-2025#__preamble__", "--knowledge-base", str(seeded_knowledge_base)],
        )

        assert "paper.lightrag-2025#__preamble__" in search_stdout
        assert "paper.lightrag-2025#__preamble__" in fetch_stdout
        assert "Preamble-only retrieval phrase." in fetch_stdout

    def test_fetch_returns_the_seeded_claim_with_its_text(self, engine: AsyncEngine, seeded_reader: str, seeded_knowledge_base: Path):
        # The seeded claim's id is `<page_id>#<block_id>` (resolver); fetch returns its text.
        stdout = _stdout_of(_ServeFactory(engine, seeded_reader, seeded_knowledge_base), ["fetch", "paper.lightrag-2025#c1", "--knowledge-base", str(seeded_knowledge_base)])

        assert "paper.lightrag-2025#c1" in stdout
        assert "claim" in stdout
        assert "UltraDomain" in stdout
