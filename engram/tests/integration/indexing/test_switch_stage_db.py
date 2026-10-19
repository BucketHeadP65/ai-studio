r"""Integration tests for the switch-decision stage against a live database.

Two tiers, deliberately (mirrors `test_repositories.py`):

* **Pure-unit tier** (`TestSignalsStatementsCompile`) indexes `SignalsQueries`'s and
  `IndexFingerprints.set_switches_statement`'s SQL against the PostgreSQL dialect with no
  server, running on every `uv run pytest`.
* **Integration tier** (`TestSwitchStageOnALiveCompile`), on the `migrated` fixture (a freshly
  `upgrade head`-migrated throwaway database), drives the real `IndexKnowledgeBaseHandler` — real
  `SqlAlchemyKnowledgeBaseUnitOfWork`, real `FilesystemKnowledgeBaseReader` — over a tiny on-disk knowledge_base:

  * two full/incremental indexes of an auto-mode, no-pin spec persist all-off switches (the
    knowledge base is far below every threshold);
  * the same knowledge base with `floor=("embeddings",)` persists `embeddings=True`;
  * a FIXED-mode index leaves a pre-seeded switches value untouched (the fixed-mode invariance
    the index's own plain fingerprint write must not violate).

  `TestSignalsQueriesMeasureAgainstRealEdges` drives `SignalsQueries.measure` directly
  against seeded pages + edges (no indexing pipeline needed for this): a small connected-cluster-
  plus-isolated-node shape pins correctness of `_SEED_NODE_SQL` and the reachability walk, and a
  120-node, edges-per-note~3, real-branching-factor shape pins that the walk stays polynomial —
  the set-based rewrite answers in well under a second at the same node count and density the
  older simple-path-enumeration walk timed out at (20s, edges_per_note~1.5, 120 notes).

  Skips the whole module when Docker is unavailable.
"""

from __future__ import annotations

import time

from typing import TYPE_CHECKING, Literal

from sqlalchemy import NullPool
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    RetrievalSwitches,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.repository import Edges, IndexFingerprints, Pages, SignalsQueries
from engram.kb.domain.schema import RetrievalSpec
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from engram.kb.contracts import KnowledgeBaseSchema


def _compiles(statement: object) -> str:
    """Index a Core statement against the asyncpg PostgreSQL dialect; return the SQL text."""
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    return str(compiled)


# ── pure-unit tier (always runs, no Docker) ─────────────────────────────────


class TestSignalsStatementsCompile:
    """`SignalsQueries`'s read SQL and the fingerprint's switches upsert index without a database."""

    def test_notes_and_kinds_counts_pages_scoped_to_the_collection(self):
        sql = _compiles(SignalsQueries.notes_and_page_types_statement("research"))

        assert "FROM kb.pages" in sql
        assert "distinct" in sql.lower()
        assert "collection" in sql

    def test_token_estimate_sums_section_body_length_over_four(self):
        sql = _compiles(SignalsQueries.token_estimate_statement("research"))

        assert "FROM kb.sections" in sql
        assert "length" in sql.lower()
        assert "coalesce" in sql.lower()

    def test_edges_count_requires_both_endpoints_in_the_collection(self):
        sql = _compiles(SignalsQueries.edges_count_statement("research"))

        assert "FROM kb.edges" in sql
        assert "src_collection" in sql
        assert "dst_collection" in sql

    def test_set_switches_creates_a_missing_row_and_on_conflict_writes_only_switches(self):
        sql = _compiles(IndexFingerprints.set_switches_statement("research", RetrievalSwitches(embeddings=True)))

        assert "INSERT INTO kb.index_fingerprint" in sql
        assert "ON CONFLICT (collection) DO UPDATE SET switches = excluded.switches" in sql
        # The conflict update names switches alone, so an indexed row keeps every business column.
        assert "indexer_version =" not in sql.split("ON CONFLICT", 1)[1]

    def test_plain_fingerprint_upsert_never_touches_switches_on_conflict(self):
        sql = _compiles(
            IndexFingerprints.upsert_statement(
                {"collection": "research", "indexer_version": "1", "spec_hash": "s", "vocabulary_hash": "r", "embed_model": "m"}
            )
        )

        assert "ON CONFLICT" in sql
        assert "switches = excluded.switches" not in sql


# ── integration tier (Docker-gated via the `migrated` fixture) ─────────────


def _spec(*, mode: Literal["fixed", "auto"], floor: tuple[str, ...] = ()) -> KnowledgeBaseSchema:
    base = research_spec()
    return base.model_copy(update={"retrieval": RetrievalSpec(mode=mode, floor=floor)})


def _tiny_knowledge_base(tmp_path: Path) -> Path:
    """A knowledge base with no unit documents -- notes=1 (the synthetic `meta.about` page), zero edges."""
    KnowledgeBaseBuilder(tmp_path).build_minimal()
    return tmp_path


async def _stored_switches(engine: AsyncEngine, collection: str) -> RetrievalSwitches:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        return await IndexFingerprints(db).get_switches(collection)


class TestSwitchStageOnALiveCompile:
    async def test_auto_mode_tiny_knowledge_base_persists_all_off_across_two_indexes(self, tmp_path: Path, migrated: str):
        knowledge_base = _tiny_knowledge_base(tmp_path)
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            handler = IndexKnowledgeBaseHandler(
                FilesystemKnowledgeBaseReader(knowledge_base, note_folders=research_spec().note_folders),
                FakeEmbedding(),
                lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine),
                FakeValidationReportSink(),
                spec=_spec(mode="auto"),
            )
            await handler.handle(IndexKnowledgeBase(collection="switchtest", embed=False))
            await handler.handle(IndexKnowledgeBase(collection="switchtest", embed=False))

            switches = await _stored_switches(engine, "switchtest")
        finally:
            await engine.dispose()

        assert switches == RetrievalSwitches()

    async def test_auto_mode_with_floor_persists_the_pinned_switch_on(self, tmp_path: Path, migrated: str):
        knowledge_base = _tiny_knowledge_base(tmp_path)
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            handler = IndexKnowledgeBaseHandler(
                FilesystemKnowledgeBaseReader(knowledge_base, note_folders=research_spec().note_folders),
                FakeEmbedding(),
                lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine),
                FakeValidationReportSink(),
                spec=_spec(mode="auto", floor=("embeddings",)),
            )
            await handler.handle(IndexKnowledgeBase(collection="switchtest-floor", embed=False))

            switches = await _stored_switches(engine, "switchtest-floor")
        finally:
            await engine.dispose()

        assert switches == RetrievalSwitches(embeddings=True)

    async def test_fixed_mode_compile_leaves_a_pre_seeded_switches_value_untouched(self, tmp_path: Path, migrated: str):
        knowledge_base = _tiny_knowledge_base(tmp_path)
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        seeded = RetrievalSwitches(embeddings=True, sql=True)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                # Seed a fingerprint row with `set` first, matching what a real prior index would
                # have left behind, then overwrite its switches to the value this test pins.
                await IndexFingerprints(db).set(
                    collection="switchtest-fixed",
                    indexer_version="0",
                    spec_hash="seed",
                    vocabulary_hash="seed",
                    embed_model="seed",
                    knowledge_base_root="",
                    spec=research_spec(),
                )
                await IndexFingerprints(db).set_switches(collection="switchtest-fixed", switches=seeded)
                await db.commit()

            handler = IndexKnowledgeBaseHandler(
                FilesystemKnowledgeBaseReader(knowledge_base, note_folders=research_spec().note_folders),
                FakeEmbedding(),
                lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine),
                FakeValidationReportSink(),
                spec=_spec(mode="fixed"),
            )
            await handler.handle(IndexKnowledgeBase(collection="switchtest-fixed", embed=False))

            switches = await _stored_switches(engine, "switchtest-fixed")
        finally:
            await engine.dispose()

        assert switches == seeded


def _page_row(page_id: str, *, collection: str) -> dict[str, object]:
    """A minimal `kb.pages` upsert row honouring every NOT NULL column."""
    return {
        "id": page_id,
        "collection": collection,
        "page_type": "entity",
        "slug": page_id.rsplit(".", 1)[-1],
        "title": page_id,
        "status": "draft",
        "card_text": page_id,
        "body_hash": "h1",
    }


def _edge_row(src_id: str, dst_id: str, *, collection: str) -> dict[str, object]:
    """A minimal `kb.edges` row, both endpoints in `collection`."""
    return {
        "src_id": src_id,
        "src_collection": collection,
        "src_kind": "entity",
        "rel": "relates_to",
        "dst_id": dst_id,
        "dst_collection": collection,
        "dst_kind": "entity",
    }


class TestSignalsQueriesMeasureAgainstRealEdges:
    """`SignalsQueries.measure` exercises `_SEED_NODE_SQL` and the reachability walk end to end.

    A connected 4-node cluster (`p.a`-`p.b`-`p.c`-`p.d`, 5 directed edges forming a cycle plus one
    chord) plus one isolated 5th node (`p.e`, no edges at all) gives an `edges_per_note` that
    clears the walk's off-threshold guard and a `giant_component` fraction with a known, assertable
    value — this is the shape C1 (the exponential simple-path walk) would have failed on at any
    real size; five nodes is enough to prove the query is *correct*, and the rewritten walk's own
    complexity is polynomial regardless of size, so this shape does not also need to be huge to be
    a meaningful regression guard.
    """

    async def test_a_connected_cluster_plus_an_isolated_node_gives_the_right_fraction(self, migrated: str):
        collection = "graphtest"
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                pages = [_page_row(f"p.{name}", collection=collection) for name in "abcde"]
                await db.execute(Pages.upsert_statement(pages))
                edges = [
                    _edge_row("p.a", "p.b", collection=collection),
                    _edge_row("p.b", "p.c", collection=collection),
                    _edge_row("p.c", "p.d", collection=collection),
                    _edge_row("p.d", "p.a", collection=collection),
                    _edge_row("p.a", "p.c", collection=collection),
                ]
                await db.execute(Edges.insert_statement(edges))
                await db.commit()

                signals = await SignalsQueries(db).measure(collection)
        finally:
            await engine.dispose()

        assert signals.notes == 5
        assert signals.edges_per_note == 1.0  # 5 edges / 5 notes
        assert signals.giant_component == 0.8  # {p.a, p.b, p.c, p.d} reached / 5 notes; p.e isolated

    async def test_a_dense_branching_graph_at_the_shape_that_used_to_time_out_completes_quickly(self, migrated: str):
        """The C1 regression guard: node count / density / branching factor the old walk timed out on.

        120 nodes on a ring, each also chorded to its +2 and +3 neighbours (3 outgoing edges per
        node, so every node has real alternative routes to reach any other -- the "branching
        factor" that makes simple-path enumeration explode). This is the shape (roughly the node
        count and edges-per-note the C1 finding measured) the old `UNION ALL` + path-array walk
        timed out on at 20s; the set-based rewrite must answer in a small fraction of that.
        """
        collection = "graphtest-dense"
        n = 120
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                pages = [_page_row(f"p.{i}", collection=collection) for i in range(n)]
                await db.execute(Pages.upsert_statement(pages))
                edges = [_edge_row(f"p.{i}", f"p.{(i + step) % n}", collection=collection) for i in range(n) for step in (1, 2, 3)]
                await db.execute(Edges.insert_statement(edges))
                await db.commit()

                started = time.monotonic()
                signals = await SignalsQueries(db).measure(collection)
                elapsed = time.monotonic() - started
        finally:
            await engine.dispose()

        assert signals.notes == n
        assert signals.edges_per_note == 3.0  # 3 * n edges / n notes
        assert signals.giant_component == 1.0  # the ring makes every node reachable from any seed
        assert elapsed < 5.0  # the old simple-path-enumeration walk timed out (20s) at this shape
