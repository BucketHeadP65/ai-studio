"""Integration: `IngestPage` decides a written collection's switches for a `mode: "auto"` spec.

Drives the real `IngestPageHandler` against a live database (the `migrated` fixture, real
`SqlAlchemyKnowledgeBaseUnitOfWork`). A written collection is never indexed, so it has no
fingerprint row before its first write. The first write under an auto spec with no pin stores an
all-off decision on a new row and lands the page unembedded. The first write under an auto spec
whose `floor` names embeddings stores embeddings on and embeds the page and its sections. The
`mode: "fixed"` invariance (embed whenever asked, decide nothing) is pinned at unit tier
(`tests/unit/engram/indexing/test_ingest.py`), which needs no database.

Skips the whole module when Docker is unavailable (via the `migrated` fixture).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import NullPool, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.indexing.contracts import IngestPage, IngestPageHandler, RetrievalSwitches, SqlAlchemyKnowledgeBaseUnitOfWork
from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.repository import IndexFingerprints
from engram.kb.contracts import AuthoringPayload
from engram.kb.domain.payload import AuthoringSection
from engram.kb.domain.schema import RetrievalSpec
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from engram.kb.contracts import KnowledgeBaseSchema

WRITTEN = "switchtest-ingest"
"""A collection no knowledge base indexes, written only through `IngestPage`."""

WRITTEN_FLOOR = "switchtest-ingest-floor"
"""A second written collection, for the spec whose `floor` pins embeddings on."""


def _auto_spec(*, floor: tuple[str, ...] = ()) -> KnowledgeBaseSchema:
    base = research_spec()
    return base.model_copy(update={"retrieval": RetrievalSpec(mode="auto", floor=floor)})


def _payload(page_id: str, spec: KnowledgeBaseSchema) -> AuthoringPayload:
    """One entity page carrying `spec`, which must be the handler's own, since a payload's spec overrides it."""
    return AuthoringPayload(
        frontmatter={
            "id": page_id,
            "type": "entity",
            "title": "Evidence audit",
            "summary": "A multi-pass audit over a contested claim.",
            "status": "draft",
            "entity_type": "technique",
        },
        sections=(AuthoringSection(heading="Definition", body="Audit a contested claim across several retrieval strategies."),),
        knowledge_base_schema=spec,
    )


class TestAnAutoModeWriteDecidesItsOwnSwitches:
    async def test_the_first_write_stores_an_all_off_decision_on_a_new_row_and_lands_the_page_unembedded(self, migrated: str):
        spec = _auto_spec()
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        embedder = FakeEmbedding()
        try:
            handler = IngestPageHandler(lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine), embedder, spec=spec)

            await handler.handle(IngestPage(collection=WRITTEN, payload=_payload("ent.audit-one", spec)))

            async with AsyncSession(engine, expire_on_commit=False) as db:
                fingerprint = await IndexFingerprints(db).get(WRITTEN)
                switches = await IndexFingerprints(db).get_switches(WRITTEN)
                page = await db.get(orm.PageORM, (WRITTEN, "ent.audit-one"))
        finally:
            await engine.dispose()

        assert fingerprint is not None
        assert switches == RetrievalSwitches()
        assert page is not None
        assert page.embedding is None
        assert embedder.texts == []

    async def test_a_floor_on_embeddings_stores_the_switch_on_and_embeds_the_first_write(self, migrated: str):
        spec = _auto_spec(floor=("embeddings",))
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        embedder = FakeEmbedding()
        try:
            handler = IngestPageHandler(lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine), embedder, spec=spec)

            await handler.handle(IngestPage(collection=WRITTEN_FLOOR, payload=_payload("ent.audit-two", spec)))

            async with AsyncSession(engine, expire_on_commit=False) as db:
                switches = await IndexFingerprints(db).get_switches(WRITTEN_FLOOR)
                page = await db.get(orm.PageORM, (WRITTEN_FLOOR, "ent.audit-two"))
                sections = (
                    await db.execute(
                        select(orm.SectionORM).where(orm.SectionORM.collection == WRITTEN_FLOOR, orm.SectionORM.page_id == "ent.audit-two")
                    )
                ).scalars().all()
        finally:
            await engine.dispose()

        assert switches == RetrievalSwitches(embeddings=True)
        assert page is not None
        assert page.embedding is not None
        assert sections
        assert all(section.embedding is not None for section in sections)
        assert embedder.texts
