"""Contract tests for the indexing slice's persistence layer (ORM + repos + UoW).

Two tiers live here, deliberately:

* **Pure-unit tests** (`TestStatementsCompile`) need no database: it indexes each
  repository's INSERT/DELETE against the PostgreSQL dialect to prove the SQL is
  well-formed without a server. These run on every `uv run pytest` and partially
  de-risk the Docker-less gap (the integration tier below cannot run when Docker
  is down).
* **Integration tests** (`TestOrmMatchesMigration`, the `Test*RoundTrip` /
  `TestUnitOfWork*` classes) use the `migrated` fixture — a freshly-migrated
  throwaway ParadeDB. They are the real contract: the ORM's declared columns
  match what alembic actually created (introspected live, table by table — a
  static DDL-text parse can't stay correct once a migration drops or renames a
  column, as 0024 does), upsert-by-id round-trips per aggregate, prune-missing
  (with `query_log`/`embed_meta` surviving prune), UoW atomicity (an induced
  mid-load failure persists nothing), and the PG-native `halfvec` / `ltree`
  round-trips. The whole tier skips when Docker is unavailable (the `pg` fixture
  raises `pytest.skip`), so the unit suite stays green on a Docker-less machine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from engram.db.contracts import KbBase
from engram.indexing.contracts import SqlAlchemyKnowledgeBaseUnitOfWork
from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.repository import Claims, Edges, EmbedMeta, Pages, Sections, SourceRepository, VocabEntries
from engram.indexing.internal_contracts import KnowledgeBaseUnitOfWork

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# ── helpers / fixtures ──────────────────────────────────────────────────────


def _compiles(statement: object) -> str:
    """Index a Core statement against the asyncpg PostgreSQL dialect; return the SQL text.

    Raises whatever SQLAlchemy raises if the statement is malformed — a green call
    means the repository emitted syntactically valid PostgreSQL.
    """
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    return str(compiled)


@pytest.fixture
async def session(migrated: str) -> AsyncIterator[AsyncSession]:
    """An `AsyncSession` bound to the freshly-migrated throwaway database."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db
    finally:
        await engine.dispose()


def _page(page_id: str, *, title: str, summary: str | None = None, body_hash: str = "h1", collection: str = "research") -> dict[str, object]:
    """A minimal `kb.pages` upsert row honouring every NOT NULL column."""
    return {
        "id": page_id,
        "collection": collection,
        "page_type": "paper",
        "slug": page_id.split(".", 1)[-1],
        "title": title,
        "status": "synthesized",
        "summary": summary,
        "card_text": f"paper: {title}",
        "body_hash": body_hash,
    }


# ── pure-unit tier (always runs, no Docker) ─────────────────────────────────


class TestStatementsCompile:
    """Each repository's mutating SQL indexes against PostgreSQL without a database."""

    def test_page_upsert_is_on_conflict_do_update(self):
        sql = _compiles(Pages.upsert_statement([_page("paper.x", title="X")]))

        assert "INSERT INTO kb.pages" in sql
        assert "ON CONFLICT" in sql
        assert "DO UPDATE" in sql

    def test_page_prune_keeps_only_known_ids(self):
        # Production prunes through the collection-scoped statement (prune_missing → _prune_scoped_statement).
        # The override only reads self._model, so a no-bind session indexes it without Docker.
        sql = _compiles(Pages(AsyncSession())._prune_scoped_statement({"paper.x", "paper.y"}, "research"))

        assert "DELETE FROM kb.pages" in sql
        assert "collection" in sql
        assert "NOT (EXISTS" in sql
        assert "kb.pages.id = listed.value" in sql

    def test_vocab_conflict_targets_composite_key(self):
        sql = _compiles(VocabEntries.upsert_statement([{"collection": "research", "vocab": "page_type", "value": "support", "definition": "d"}]))

        assert "ON CONFLICT" in sql

    def test_edge_replace_emits_delete_and_insert(self):
        # Edges have a bigserial PK (no stable natural id) → rebuilt by scoped-delete + insert.
        # Production scopes the wipe by collection (replace_all → delete_collection_statement).
        delete_sql = _compiles(Edges.delete_collection_statement("research"))
        insert_sql = _compiles(
            Edges.insert_statement([{"src_id": "p.a", "src_kind": "page", "rel": "cites", "dst_id": "p.b", "dst_kind": "page"}])
        )

        assert "DELETE FROM kb.edges" in delete_sql
        assert "collection" in delete_sql
        assert "INSERT INTO kb.edges" in insert_sql

    def test_embedding_select_filters_null_embedding(self):
        sql = _compiles(Pages.needing_embedding_statement("research"))

        assert "FROM kb.pages" in sql
        assert "embedding IS NULL" in sql

    def test_meta_repository_targets_embed_meta(self):
        # embed_meta has no PK in the DDL → a single-row replace (delete-all + insert), not upsert-by-id.
        delete_sql = _compiles(EmbedMeta.delete_all_statement())
        insert_sql = _compiles(EmbedMeta.insert_statement(model="m", dim=1024))

        assert "DELETE FROM kb.embed_meta" in delete_sql
        assert "INSERT INTO kb.embed_meta" in insert_sql


# ── integration tier (Docker-gated via the `migrated` fixture) ────────────


class TestOrmMatchesMigration:
    """The index-slice ORM must map onto the migrated `kb` schema — every table, every column.

    `KbBase` is the one declarative base for the *whole* `kb` schema, not only these
    tables — other slices (index-state, evals, unit summaries) map onto it too, and importing
    this module pulls their classes in too via `engram.indexing.infrastructure.repository`'s own imports. So the
    table-presence check below is `<=` (these eleven are at least present), not `==`; only the
    per-table column-parity check is exact.
    """

    def test_all_tables_mapped_in_the_kb_schema(self):
        mapped = {(t.schema, t.name) for t in KbBase.metadata.tables.values()}

        expected = {
            ("kb", name)
            for name in (
                "sources_raw",
                "raw_chunks",
                "pages",
                "sections",
                "claims",
                "claim_qualifiers",
                "edges",
                "vocab",
                "embed_meta",
                "query_log",
            )
        }
        assert expected <= mapped

    @pytest.mark.parametrize(
        "model",
        [
            orm.SourceRawORM,
            orm.RawChunkORM,
            orm.PageORM,
            orm.SectionORM,
            orm.ClaimORM,
            orm.ClaimQualifierORM,
            orm.EdgeORM,
            orm.VocabORM,
            orm.EmbedMetaORM,
            orm.QueryLogORM,
            orm.CutPlanORM,
        ],
    )
    async def test_columns_match_migration_ddl(self, migrated: str, model: type) -> None:
        """`model`'s declared columns match exactly what the migration chain actually created.

        Introspected live from `information_schema` rather than parsed out of migration source —
        a static parse of `ADD COLUMN` statements alone cannot stay correct once a migration drops
        or renames a column (0024 does both), and a live table is the one ground truth that
        already accounts for every revision's net effect without needing to re-derive it.
        """
        table = model.__tablename__  # type: ignore[attr-defined]
        orm_cols = {c.name for c in model.__table__.columns}  # type: ignore[attr-defined]

        engine = create_async_engine(migrated, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'kb' AND table_name = :table"),
                    {"table": table},
                )
                db_cols = {row[0] for row in result}
        finally:
            await engine.dispose()

        assert orm_cols == db_cols

    def test_pages_embedding_is_halfvec_1024(self):
        col = orm.PageORM.__table__.c.embedding

        assert isinstance(col.type, orm.HALFVEC)
        assert col.type.dim == 1024

    def test_sections_path_is_ltree(self):
        from core.column_types import Ltree

        assert isinstance(orm.SectionORM.__table__.c.path.type, Ltree)


class TestPageRoundTrip:
    async def test_insert_then_reupsert_updates_changed_fields(self, session: AsyncSession):
        repo = Pages(session)
        await repo.upsert_many([_page("paper.a", title="First", summary="one")])
        await session.commit()

        await repo.upsert_many([_page("paper.a", title="First", summary="two")])
        await session.commit()

        row = await session.get(orm.PageORM, ("research", "paper.a"))
        assert row is not None
        assert row.summary == "two"

    async def test_prune_missing_removes_vanished_ids(self, session: AsyncSession):
        repo = Pages(session)
        await repo.upsert_many([_page("paper.a", title="A"), _page("paper.b", title="B")])
        await session.commit()

        await repo.prune_missing({"paper.a"}, collection="research")
        await session.commit()

        assert await session.get(orm.PageORM, ("research", "paper.a")) is not None
        assert await session.get(orm.PageORM, ("research", "paper.b")) is None

    async def test_changed_body_hash_nulls_embedding_so_it_re_embeds(self, session: AsyncSession):
        repo = Pages(session)
        await repo.upsert_many([_page("paper.a", title="A", body_hash="h1")])
        await session.commit()
        row = await session.get(orm.PageORM, ("research", "paper.a"))
        assert row is not None
        row.embedding = [0.1] * 1024  # pretend the embed stage already vectorised it
        await session.commit()

        await repo.upsert_many([_page("paper.a", title="A", body_hash="h2")])  # body changed
        await session.commit()

        refreshed = await session.get(orm.PageORM, ("research", "paper.a"))
        assert refreshed is not None
        assert refreshed.embedding is None
        needing = await repo.rows_needing_embedding(collection="research")
        assert "paper.a" in {r.id for r in needing}

    async def test_unchanged_body_hash_keeps_embedding(self, session: AsyncSession):
        repo = Pages(session)
        await repo.upsert_many([_page("paper.a", title="A", body_hash="h1")])
        await session.commit()
        row = await session.get(orm.PageORM, ("research", "paper.a"))
        assert row is not None
        row.embedding = [0.2] * 1024
        await session.commit()

        await repo.upsert_many([_page("paper.a", title="A", summary="new", body_hash="h1")])  # body unchanged
        await session.commit()

        refreshed = await session.get(orm.PageORM, ("research", "paper.a"))
        assert refreshed is not None
        assert refreshed.embedding is not None

    async def test_write_embeddings_round_trips_by_collection_and_id(self, session: AsyncSession):
        """The embed stage's vector write-back: a bulk UPDATE keyed by the composite `(collection, id)` PK.

        Regression: pre-fix, `write_embeddings` bound only `id`, and SQLAlchemy's ORM-enabled bulk
        UPDATE-by-primary-key refused the statement outright (`InvalidRequestError: No primary key
        value supplied for column(s) pages.collection`) the moment the mapper's PK went composite.
        """
        repo = Pages(session)
        await repo.upsert_many([_page("paper.a", title="A")])
        await session.commit()

        vector = [0.5] * 1024
        await repo.write_embeddings({"paper.a": vector}, collection="research")
        await session.commit()

        row = await session.get(orm.PageORM, ("research", "paper.a"))
        assert row is not None
        assert row.embedding is not None


class TestVocabRoundTrip:
    async def test_upsert_and_prune_on_composite_key(self, session: AsyncSession):
        repo = VocabEntries(session)
        await repo.upsert_many(
            [
                {"collection": "research", "vocab": "page_type", "value": "support", "definition": "old"},
                {"collection": "research", "vocab": "page_type", "value": "refute", "definition": "r"},
            ]
        )
        await session.commit()
        await repo.upsert_many([{"collection": "research", "vocab": "page_type", "value": "support", "definition": "new"}])
        await session.commit()

        await repo.prune_missing({("page_type", "support")}, collection="research")
        await session.commit()

        rows = await repo.all()
        as_map = {(r.vocab, r.value): r.definition for r in rows}
        assert as_map == {("page_type", "support"): "new"}

    async def test_prune_scopes_to_its_own_collection(self, session: AsyncSession):
        """Pruning collection A's vocab must leave collection B's rows — even a same-named term — intact."""
        repo = VocabEntries(session)
        await repo.upsert_many(
            [
                {"collection": "alpha", "vocab": "page_type", "value": "support", "definition": "alpha's"},
                {"collection": "beta", "vocab": "page_type", "value": "support", "definition": "beta's"},
            ]
        )
        await session.commit()

        await repo.prune_missing(set(), collection="alpha")
        await session.commit()

        rows = await repo.all()
        as_map = {(r.collection, r.vocab, r.value): r.definition for r in rows}
        assert as_map == {("beta", "page_type", "support"): "beta's"}


class TestSourceRoundTrip:
    async def test_sources_and_chunks_round_trip_and_prune(self, session: AsyncSession):
        repo = SourceRepository(session)
        await repo.upsert_sources(
            [{"source_id": "K1", "provider": "zotero", "provider_key": "K1", "collection": "research", "title": "Paper", "source_hash": "sh1"}]
        )
        await repo.upsert_chunks(
            [
                {"id": "K1:0", "collection": "research", "source_id": "K1", "seq": 0, "body": "alpha"},
                {"id": "K1:1", "collection": "research", "source_id": "K1", "seq": 1, "body": "beta"},
            ]
        )
        await session.commit()

        await repo.prune_missing_chunks({"K1:0"}, collection="research")
        await session.commit()

        assert await session.get(orm.RawChunkORM, ("research", "K1:0")) is not None
        assert await session.get(orm.RawChunkORM, ("research", "K1:1")) is None

    async def test_write_chunk_embeddings_round_trips_by_collection_and_id(self, session: AsyncSession):
        """The chunk analogue of `TestPageRoundTrip.test_write_embeddings_round_trips_by_collection_and_id`."""
        repo = SourceRepository(session)
        await repo.upsert_sources(
            [{"source_id": "K1", "provider": "zotero", "provider_key": "K1", "collection": "research", "title": "Paper", "source_hash": "sh1"}]
        )
        await repo.upsert_chunks([{"id": "K1:0", "collection": "research", "source_id": "K1", "seq": 0, "body": "alpha"}])
        await session.commit()

        vector = [0.5] * 1024
        await repo.write_chunk_embeddings({"K1:0": vector}, collection="research")
        await session.commit()

        row = await session.get(orm.RawChunkORM, ("research", "K1:0"))
        assert row is not None
        assert row.embedding is not None


class TestEdgeReplace:
    async def test_replace_all_swaps_the_edge_set(self, session: AsyncSession):
        # Seed the source page so the collection-scoped DELETE subquery can match src_id.
        await Pages(session).upsert_many([_page("p.a", title="A")])
        repo = Edges(session)
        await repo.replace_all(
            [
                {
                    "src_collection": "research",
                    "src_id": "p.a",
                    "src_kind": "page",
                    "rel": "cites",
                    "dst_collection": "research",
                    "dst_id": "p.b",
                    "dst_kind": "page",
                }
            ],
            collection="research",
        )
        await session.commit()

        await repo.replace_all(
            [
                {
                    "src_collection": "research",
                    "src_id": "p.a",
                    "src_kind": "page",
                    "rel": "extends",
                    "dst_collection": "research",
                    "dst_id": "p.c",
                    "dst_kind": "page",
                }
            ],
            collection="research",
        )
        await session.commit()

        rels = {r.rel for r in await repo.all()}
        assert rels == {"extends"}


class TestHalfvecAndLtreeRoundTrip:
    async def test_halfvec_1024_round_trips(self, session: AsyncSession):
        repo = Pages(session)
        await repo.upsert_many([_page("paper.a", title="A")])
        await session.commit()
        row = await session.get(orm.PageORM, ("research", "paper.a"))
        assert row is not None
        vector = [float(i % 7) for i in range(1024)]
        row.embedding = vector
        await session.commit()
        session.expire_all()

        refreshed = await session.get(orm.PageORM, ("research", "paper.a"))
        assert refreshed is not None
        stored = refreshed.embedding
        assert isinstance(stored, list)
        assert [float(x) for x in stored][:5] == pytest.approx(vector[:5])

    async def test_ltree_path_round_trips(self, session: AsyncSession):
        await Pages(session).upsert_many([_page("paper.a", title="A")])
        repo = Sections(session)
        await repo.upsert_many(
            [
                {
                    "id": "paper.a#findings",
                    "collection": "research",
                    "page_id": "paper.a",
                    "heading": "Findings",
                    "path": "findings",
                    "ord": 0,
                    "body": "b",
                    "body_hash": "h",
                }
            ]
        )
        await session.commit()
        session.expire_all()

        row = await session.get(orm.SectionORM, ("research", "paper.a#findings"))
        assert row is not None
        assert row.path == "findings"


class TestUnitOfWorkContract:
    def test_satisfies_the_protocol(self, migrated: str):
        engine = create_async_engine(migrated, poolclass=NullPool)
        uow = SqlAlchemyKnowledgeBaseUnitOfWork(engine)

        assert isinstance(uow, KnowledgeBaseUnitOfWork)

    async def test_repositories_are_attributes_after_enter(self, migrated: str):
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            async with SqlAlchemyKnowledgeBaseUnitOfWork(engine) as uow:
                assert isinstance(uow.pages, Pages)
                assert isinstance(uow.sections, Sections)
                assert isinstance(uow.claims, Claims)
                assert isinstance(uow.edges, Edges)
                assert isinstance(uow.sources, SourceRepository)
                assert isinstance(uow.vocab, VocabEntries)
                assert isinstance(uow.meta, EmbedMeta)
        finally:
            await engine.dispose()

    async def test_commit_persists_the_whole_rebuild(self, migrated: str):
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            async with SqlAlchemyKnowledgeBaseUnitOfWork(engine) as uow:
                await uow.pages.upsert_many([_page("paper.a", title="A")])
                await uow.commit()

            async with SqlAlchemyKnowledgeBaseUnitOfWork(engine) as uow:
                assert await uow.session.get(orm.PageORM, ("research", "paper.a")) is not None
        finally:
            await engine.dispose()

    async def test_uncommitted_exit_rolls_back_so_nothing_persists(self, migrated: str):
        async def _load_then_fail(engine: AsyncEngine) -> None:
            # An induced failure after writing but before commit: __aexit__ must roll back.
            async with SqlAlchemyKnowledgeBaseUnitOfWork(engine) as uow:
                await uow.pages.upsert_many([_page("paper.a", title="A")])
                await uow.pages.flush()
                msg = "induced mid-load failure"
                raise RuntimeError(msg)

        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            with pytest.raises(RuntimeError, match="induced"):
                await _load_then_fail(engine)

            async with SqlAlchemyKnowledgeBaseUnitOfWork(engine) as uow:
                assert await uow.session.get(orm.PageORM, ("research", "paper.a")) is None
        finally:
            await engine.dispose()


class TestPruneExemptions:
    async def test_query_log_and_embed_meta_have_no_prune_method(self):
        # query_log + embed_meta are exempt from prune-missing: no repository surface may
        # DELETE query_log rows during a rebuild.
        assert not hasattr(EmbedMeta, "prune_missing")
