"""Integration: auto-mode search routes by the stored embeddings switch and says what answered.

Reads go through the `reader_url` fixture (the `kb_reader` role, `migrated` underneath, real
`embedded_retrieval`): an auto-mode knowledge base whose embeddings switch has never been decided (the
state every collection starts in, before any index has run) answers lexical — no vector leg,
every hit's `answered_by == "lexical"`. Flipping the stored switch on through
`IndexFingerprints.set_switches` (the same method `SwitchStage` calls, per
`kb.index_fingerprint`'s admin side) makes the very next search on the SAME open reader
answer hybrid, `answered_by == "hybrid"` — no rebuild, the switch is read fresh per call.

Skips the whole module when Docker is unavailable (via the `migrated` fixture).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.indexing.contracts import IngestPage, IngestPageHandler, RetrievalSwitches, SqlAlchemyKnowledgeBaseUnitOfWork
from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.repository import IndexFingerprints
from engram.kb.contracts import AuthoringPayload
from engram.kb.domain.payload import AuthoringSection
from engram.kb.domain.schema import RetrievalSpec
from engram.retrieval.domain import GraphResult, SearchResult
from engram.retrieval.infrastructure.embedded import embedded_retrieval
from engram.testing.fakes.embedding import FakeEmbedding, FakeQueryEmbedding
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from engram.kb.contracts import KnowledgeBaseSchema

WRITTEN = "switchtest-routing"
"""A collection no knowledge base indexes — the only page_type `IngestPage` accepts."""

QUERY = "contested claim"
"""Present verbatim in the seeded page, so both the BM25 and the vector leg can surface it."""


def _auto_spec() -> KnowledgeBaseSchema:
    base = research_spec()
    return base.model_copy(update={"retrieval": RetrievalSpec(mode="auto", collection=WRITTEN)})


def _payload(spec: KnowledgeBaseSchema) -> AuthoringPayload:
    """The page a caller ingests, validated against its own `spec`.

    `spec` must match the handler's own (`IngestPageHandler`'s docstring: a payload carrying its
    own spec overrides the handler's at `handle()` time), so callers pass their own `_auto_spec()`/
    `_auto_graph_spec()` — never a plain `research_spec()`, whose `mode: "fixed"` would silently
    downgrade every call and defeat these tests' whole switch-obedience point.
    """
    return AuthoringPayload(
        frontmatter={
            "id": "ent.audit-one",
            "type": "entity",
            "title": "Evidence audit",
            "summary": "A multi-pass audit over a contested claim.",
            "status": "draft",
            "entity_type": "technique",
        },
        sections=(AuthoringSection(heading="Definition", body="Audit a contested claim across several retrieval strategies."),),
        knowledge_base_schema=spec,
    )


async def _seed_page(migrated: str, spec: KnowledgeBaseSchema) -> None:
    """Write one page into `WRITTEN` through the real ingest path (no index involved)."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        handler = IngestPageHandler(lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine), FakeEmbedding(), spec=spec)
        await handler.handle(IngestPage(collection=WRITTEN, payload=_payload(spec)))
    finally:
        await engine.dispose()


async def _turn_the_embeddings_switch_on(migrated: str) -> None:
    """Seed a fingerprint row then flip embeddings on."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await IndexFingerprints(db).set(
                collection=WRITTEN,
                indexer_version="0",
                spec_hash="seed",
                vocabulary_hash="seed",
                embed_model="seed",
                knowledge_base_root="",
            )
            await IndexFingerprints(db).set_switches(collection=WRITTEN, switches=RetrievalSwitches(embeddings=True))
            await db.commit()
    finally:
        await engine.dispose()


class TestAutoModeSearchRoutesByTheStoredSwitch:
    async def test_switch_off_then_on_flips_the_very_next_search(self, migrated: str, reader_url: str):
        spec = _auto_spec()
        await _seed_page(migrated, spec)

        async with embedded_retrieval(reader_url, spec=spec, embed_query=FakeQueryEmbedding()) as knowledge_base:
            lexical = await knowledge_base.direct_call_tool("search", {"query": QUERY, "k": 5})
            assert isinstance(lexical, SearchResult)
            assert lexical.hits, "the seeded page must be found by its BM25 leg alone"
            assert all(hit.answered_by == "lexical" for hit in lexical.hits)

            await _turn_the_embeddings_switch_on(migrated)

            hybrid = await knowledge_base.direct_call_tool("search", {"query": QUERY, "k": 5})
            assert isinstance(hybrid, SearchResult)
            assert hybrid.hits
            assert "Answered by hybrid." in hybrid.next_steps.summary, hybrid.next_steps.summary


GRAPH_WRITTEN = "switchtest-graph"
"""A separate collection no knowledge base indexes, isolated from the search-switch test's own."""


def _auto_graph_spec() -> KnowledgeBaseSchema:
    base = research_spec()
    return base.model_copy(update={"retrieval": RetrievalSpec(mode="auto", collection=GRAPH_WRITTEN)})


async def _turn_the_graph_switch_on(migrated: str) -> None:
    """Seed a fingerprint row then flip graph on."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await IndexFingerprints(db).set(
                collection=GRAPH_WRITTEN,
                indexer_version="0",
                spec_hash="seed",
                vocabulary_hash="seed",
                embed_model="seed",
                knowledge_base_root="",
            )
            await IndexFingerprints(db).set_switches(collection=GRAPH_WRITTEN, switches=RetrievalSwitches(graph=True))
            await db.commit()
    finally:
        await engine.dispose()


class TestAutoModeGraphToolsAppearWhenEarned:
    async def test_switch_off_then_on_exposes_the_graph_tools_and_they_reach_api_graph_expand(self, migrated: str, reader_url: str):
        spec = _auto_graph_spec()
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            handler = IngestPageHandler(lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine), FakeEmbedding(), spec=spec)
            await handler.handle(IngestPage(collection=GRAPH_WRITTEN, payload=_payload(spec)))
        finally:
            await engine.dispose()

        async with embedded_retrieval(reader_url, spec=spec, embed_query=FakeQueryEmbedding()) as knowledge_base:
            off_names = {tool.name for tool in await knowledge_base.list_tools()}
            assert off_names == {"search", "fetch"}

            await _turn_the_graph_switch_on(migrated)

            on_names = {tool.name for tool in await knowledge_base.list_tools()}
            assert on_names == {"search", "fetch", "graph_expand", "find_paths"}

            result = await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["ent.audit-one"], "hops": 1})
            assert isinstance(result, GraphResult)
            assert any(node.id == "ent.audit-one" for node in result.nodes)
