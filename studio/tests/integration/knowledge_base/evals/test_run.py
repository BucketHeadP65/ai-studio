r"""The evals loop end-to-end over a real, indexed ParadeDB.

Drives the full retrieval-eval pipeline against a real database: a mini `KnowledgeBaseBuilder`
knowledge base → ``memory index`` (real PG Unit-of-Work + :class:`FakeEmbedding`, the e2e seeding shape, as
in `tests/integration/test_search.py`) → the single-source retrieval toolset
(:func:`build_retrieval_toolset`) bound over the production read-only
:class:`~engram.retrieval.infrastructure.gateway.ReadOnlySqlGateway` (the ``kb_reader`` role) → the agent-under-test
(:func:`build_agent` over a network-free ``TestModel``) → ``Dataset.evaluate`` with the
:class:`~engram.evals.harness.contracts.RetrievalMetrics` evaluator.

The point is that the **metrics are computed end-to-end** against real infrastructure: the toolset
talks to a real ParadeDB through the reader role, and `RetrievalMetrics` reads the captured
OpenTelemetry `span_tree` (so this also proves the local span-recording seam keeps the evaluator
from raising `SpanTreeRecordingError` on a tokenless run). The agent uses `TestModel` so the run
is deterministic and free — it synthesizes a :class:`~engram.evals.shared.domain.RetrievalAnswer`, so the
*scores* are arbitrary; the assertions check that every metric was produced for every case, not that
recall is high.

The whole module skips when Docker is unavailable (the `pg` fixture, via `migrated`, raises
`pytest.skip`). Tests are synchronous: the `indexed` fixture drives the Typer `index` command
(`asyncio.run` internally), and the eval body wraps its own `asyncio.run` — the same shape as
`tests/integration/test_search.py`.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.evals.generation.contracts import AuthoredCase, build_dataset
from engram.evals.harness.contracts import build_agent, make_task
from engram.evals.shared.domain import ExpectedQa, QaCaseMeta, QaInput
from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler, KnowledgeBaseReader
from engram.indexing.infrastructure import orm
from engram.kb.contracts import load_knowledge_base_schema
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.queries import FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.retrieval.tools import RetrievalHandlers, build_retrieval_toolset
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.wiring.contracts import AdminSqlGateway
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

    from pydantic_evals.reporting import EvaluationReport
    from sqlalchemy.ext.asyncio import AsyncEngine

runner = CliRunner()

_INSTRUCTIONS = "Answer the question using the retrieval tools, then cite the page ids you relied on."

# Two distinct, searchable papers — their ids are the golden expected_source_ids below.
_PAPER_GRAPH = """\
---
id: paper.graph-cost
type: paper
title: Graph Indexing Token Cost
summary: Building a knowledge graph index incurs substantial token cost during construction.
status: synthesized
zotero_key: GRAPHKEY1
year: 2025
paper_type: method
source_hash: g001
tasks: [long-doc-qa]
stages: [indexing]
---

## TL;DR

Graph index construction spends many tokens extracting entities and relations.

## Findings

- Graph indexing incurs substantial token cost during construction. ^c1
    - loc:: §3.1
    - kind:: observation
"""

_PAPER_CACHE = """\
---
id: paper.vector-cache
type: paper
title: Vector Caching of Hot Passages
summary: Caching hot passage embedding vectors slashes retrieval latency on repeat queries.
status: synthesized
zotero_key: CACHEKEY1
year: 2024
paper_type: method
tasks: [multi-hop-qa]
stages: [retrieval]
source_hash: c001
---

## TL;DR

A vector cache keeps frequently retrieved passage embeddings hot.

## Findings

- Caching hot passage vectors reduces retrieval latency substantially. ^c1
    - loc:: §4.2
    - kind:: observation
"""


def _mirror(source_key: str, source_hash: str, body: str) -> str:
    """A minimal raw mirror — its only job is to satisfy the pages.zotero_key FK."""
    return f"---\nsource_key: {source_key}\nprovider: example-library\nsource_hash: {source_hash}\n---\n# Mirror\n\n## 1 Body\n\n{body}\n"


def _build_knowledge_base(root: Path) -> Path:
    """Lay down the two papers, their mirrors, registries, and meta stubs."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Papers", "graph-cost", _PAPER_GRAPH)
    builder.add_unit_document("Papers", "vector-cache", _PAPER_CACHE)
    builder.add_source_mirror("graph-cost", _mirror("GRAPHKEY1", "g001", "Graph index construction token cost."))
    builder.add_source_mirror("vector-cache", _mirror("CACHEKEY1", "c001", "Vector caching of hot passages."))
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


class _Factory:
    """A `ctx.obj` factory whose bus runs a real-PG, fake-embed index handler (e2e shape)."""

    def __init__(self, engine: AsyncEngine, knowledge_base: Path) -> None:
        self._reader = FilesystemKnowledgeBaseReader(knowledge_base)
        handler = IndexKnowledgeBaseHandler(
            self._reader, FakeEmbedding(), KnowledgeBaseUnitOfWorkFactory(engine), FileValidationReportSink(knowledge_base), spec=load_knowledge_base_schema(knowledge_base)
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
def indexed(migrated: str, tmp_path: Path) -> str:
    """Index the mini knowledge base into the freshly-migrated DB; return the admin URL."""
    knowledge_base = _build_knowledge_base(tmp_path)
    engine = create_async_engine(migrated, poolclass=NullPool)

    async def _register() -> None:
        await orm.register_pgvector(engine)

    asyncio.run(_register())
    result = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))
    assert result.exit_code == 0, result.stdout
    return migrated


@pytest.fixture
def reader(indexed: str, reader_url: str) -> str:
    """The reader URL over the *indexed* DB (forces indexing before any eval read)."""
    del indexed  # ordering dependency: index must run before the eval reads
    return reader_url


def _enable_local_span_recording() -> None:
    """Local-only Logfire span recording so `RetrievalMetrics` can read `span_tree` tokenlessly."""
    import logfire

    logfire.configure(send_to_logfire=False, console=False)
    logfire.instrument_pydantic_ai()


def _run[T](coro: Coroutine[object, object, T]) -> T:
    """Drive a coroutine from a synchronous test body (no loop is running)."""
    return asyncio.run(coro)


async def _evaluate(reader_url: str) -> EvaluationReport:
    """Build the toolset over the indexed DB, the TestModel agent, and run the golden dataset."""
    from pydantic_ai.models.test import TestModel

    engine = create_async_engine(reader_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        gateway = ReadOnlySqlGateway(engine)
        handlers = RetrievalHandlers(
            search=SearchHandler(gateway, FakeEmbedding()),
            fetch=FetchHandler(gateway, "research"),
            graph_expand=GraphExpandHandler(gateway, "research"),
            find_paths=GraphPathsHandler(gateway, "research"),
        )
        toolset = build_retrieval_toolset(handlers, collection="research")
        agent = build_agent(TestModel(call_tools=[]), instructions=_INSTRUCTIONS, toolset=toolset)
        dataset = build_dataset(
            [
                AuthoredCase(
                    question=QaInput(question="What is the dominant cost of building a graph index?"),
                    expected=ExpectedQa(answer="Token cost during construction.", expected_source_ids=("paper.graph-cost",)),
                    meta=QaCaseMeta(anchor_page_id="paper.graph-cost", difficulty="easy"),
                ),
                AuthoredCase(
                    question=QaInput(question="How does a vector cache help retrieval latency?"),
                    expected=ExpectedQa(answer="It keeps hot passage vectors cached.", expected_source_ids=("paper.vector-cache",)),
                    meta=QaCaseMeta(anchor_page_id="paper.vector-cache", difficulty="easy"),
                ),
            ]
        )
        return await dataset.evaluate(make_task(agent))
    finally:
        await engine.dispose()


_EXPECTED_METRICS = {"recall@1", "recall@5", "recall@10", "precision@1", "precision@5", "precision@10", "mrr", "n_tool_calls"}


class TestEvalsLoopOverIndexedDb:
    def test_run_scores_every_case_with_the_retrieval_metrics(self, reader: str):
        _enable_local_span_recording()

        report = _run(_evaluate(reader))

        # The loop ran end-to-end against real ParadeDB and produced one scored case per golden case.
        assert len(report.cases) == 2
        for case in report.cases:
            score_names = set(case.scores)
            assert score_names >= _EXPECTED_METRICS, f"missing metrics for {case.name}: {_EXPECTED_METRICS - score_names}"
