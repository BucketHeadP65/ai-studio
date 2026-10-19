r"""`KnowledgeBasesHandler` over a real, migrated ParadeDB — the indexed-collections listing.

The read path: seed `kb.pages` across two distinct `collection` values and stamp a
`index_fingerprint` row for each, directly through the indexing slice's repositories — the same
direct-seeding idiom as `tests/integration/test_repositories.py` and `tests/integration/test_graph.py`
— then query through the production `ReadOnlySqlGateway` and the new
:class:`~engram.retrieval.queries.KnowledgeBasesHandler`.

`KnowledgeBasesHandler` reads `kb.pages`/`kb.index_fingerprint` directly (not through the
`api` schema), so in production it is wired to the **admin** gateway, the same posture
`StatusHandler` uses; the `migrated` fixture already yields the admin-privileged URL, so it is
passed straight through here.

The whole module skips when Docker is unavailable (the `pg` fixture, via `migrated`, raises
`pytest.skip`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.repository import IndexFingerprints, Pages
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.queries import KnowledgeBasesHandler

if TYPE_CHECKING:
    from engram.retrieval.queries import Collections

_RESEARCH_COMPILED_AT = datetime(2026, 8, 1, tzinfo=UTC)
_NOTEBOOK_COMPILED_AT = datetime(2026, 8, 15, tzinfo=UTC)


def _page_row(page_id: str, *, collection: str, page_type: str, title: str) -> dict[str, object]:
    """A minimal `kb.pages` upsert row honouring every NOT NULL column, scoped to `collection`."""
    return {
        "id": page_id,
        "collection": collection,
        "page_type": page_type,
        "slug": page_id.split(".", 1)[-1],
        "title": title,
        "status": "verified",
        "summary": None,
        "card_text": f"{page_type}: {title}",
        "body_hash": f"h-{page_id}",
    }


_RESEARCH_PAGES: tuple[dict[str, object], ...] = (
    _page_row("paper.graph", collection="research", page_type="paper", title="Graph Indexing"),
    _page_row("ent.graphrag", collection="research", page_type="entity", title="GraphRAG"),
)
_NOTEBOOK_PAGES: tuple[dict[str, object], ...] = (_page_row("page.release-checklist", collection="notebook", page_type="page", title="Release Checklist"),)


async def _seed(engine_url: str) -> None:
    """Seed two collections' pages and stamp a `index_fingerprint` row for each, in one session."""
    engine = create_async_engine(engine_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await Pages(db).upsert_many([*_RESEARCH_PAGES, *_NOTEBOOK_PAGES])
            await IndexFingerprints(db, now_fn=lambda: _RESEARCH_COMPILED_AT).set(
                collection="research",
                indexer_version="v1",
                spec_hash="s1",
                vocabulary_hash="r1",
                embed_model="m1",
                knowledge_base_root="",
            )
            await IndexFingerprints(db, now_fn=lambda: _NOTEBOOK_COMPILED_AT).set(
                collection="notebook",
                indexer_version="v1",
                spec_hash="s1",
                vocabulary_hash="r1",
                embed_model="m1",
                knowledge_base_root="",
            )
            await db.commit()
    finally:
        await engine.dispose()


@pytest.fixture
async def collections_db(migrated: str) -> str:
    """Seed the migrated DB with two indexed collections; return its (admin) URL."""
    await _seed(migrated)
    return migrated


async def _collections(db_url: str) -> Collections:
    """Run `KnowledgeBasesHandler` through a gateway over `db_url` (the admin URL, per its grant posture)."""
    engine = create_async_engine(db_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        return await KnowledgeBasesHandler(admin_gateway=ReadOnlySqlGateway(engine))()
    finally:
        await engine.dispose()


class TestKnowledgeBasesHandler:
    async def test_two_compiled_collections_are_both_listed(self, collections_db: str):
        result = await _collections(collections_db)

        assert {info.name for info in result.collections} == {"research", "notebook"}

    async def test_each_collection_reports_its_own_page_count_and_kind_mix(self, collections_db: str):
        result = await _collections(collections_db)

        research = next(info for info in result.collections if info.name == "research")
        assert research.pages == 2
        assert set(research.page_types) == {"paper", "entity"}

        notebook = next(info for info in result.collections if info.name == "notebook")
        assert notebook.pages == 1
        assert notebook.page_types == ("page",)

    async def test_each_collection_reports_its_own_last_compiled_time(self, collections_db: str):
        result = await _collections(collections_db)

        research = next(info for info in result.collections if info.name == "research")
        notebook = next(info for info in result.collections if info.name == "notebook")
        assert research.last_indexed == _RESEARCH_COMPILED_AT
        assert notebook.last_indexed == _NOTEBOOK_COMPILED_AT
