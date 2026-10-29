"""`SqlAlchemySummaryUnitOfWork.pending_units` scopes to its own collection, not a bare unit id.

`kb.pages` / `kb.unit_summaries` are both `(collection, id)`-keyed as of revision 0025, so
two collections may legally share a bare unit id. `pending_pages_statement` /
`pending_sections_statement` pair the `unit_summaries` join on `(collection, unit_id)`, not
`unit_id` alone — this is the regression test for that fix: two collections indexing the same
page id must each queue and clear their own pending summary independently. A run whose summarizer raises still gives
its connection back to the pool.
"""

import pytest

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import QueuePool

from engram.indexing.contracts import SummaryStage
from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.repository import Pages
from engram.indexing.infrastructure.summary_uow import SqlAlchemySummaryUnitOfWork


class _RaisingSummarizer:
    """A summarizer whose model call fails."""

    model_id = "raising-summarizer"

    async def summarize(self, *, title: str, body: str) -> str:
        del title, body
        msg = "the summary model did not answer"
        raise RuntimeError(msg)


def _page(page_id: str, collection: str) -> dict[str, object]:
    """A minimal `kb.pages` upsert row honouring every NOT NULL column."""
    return {
        "id": page_id,
        "collection": collection,
        "page_type": "paper",
        "slug": page_id.split(".", 1)[-1],
        "title": "Shared",
        "status": "synthesized",
        "summary": None,
        "card_text": f"body for {collection}",
        "body_hash": "h1",
    }


async def test_two_collections_with_the_same_unit_id_summarize_independently(migrated: str) -> None:
    engine: AsyncEngine = create_async_engine(migrated)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await Pages(session).upsert_many([_page("shared.id", "alpha"), _page("shared.id", "beta")])
            await session.commit()

        uow = SqlAlchemySummaryUnitOfWork(engine)
        alpha_pending = await uow.pending_units("alpha")
        beta_pending = await uow.pending_units("beta")
        assert {u.unit_id for u in alpha_pending} == {"shared.id"}
        assert {u.unit_id for u in beta_pending} == {"shared.id"}

        # alpha's page gets an active summary; beta's identically-id'd page must stay untouched.
        await uow.upsert_summary(
            {
                "unit_id": "shared.id",
                "unit_kind": "page",
                "collection": "alpha",
                "summary": "Alpha's own summary.",
                "status": "pending",
                "model": "test-model",
                "source_hash": "h1",
            }
        )
        await uow.commit()

        # `pending_units` issues a fresh SELECT each call (no ORM identity caching to go stale
        # against), so the same UoW instance reads post-commit state correctly.
        alpha_after = await uow.pending_units("alpha")
        beta_after = await uow.pending_units("beta")
        await uow.close()
        assert alpha_after == []  # alpha's own current summary now covers it
        assert {u.unit_id for u in beta_after} == {"shared.id"}  # beta's page is still unsummarized
    finally:
        await engine.dispose()


async def test_a_run_whose_summarizer_raises_leaves_no_connection_checked_out(migrated: str) -> None:
    engine: AsyncEngine = create_async_engine(migrated)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await Pages(session).upsert_many([_page("shared.id", "alpha")])
            await session.commit()
        stage = SummaryStage(lambda: SqlAlchemySummaryUnitOfWork(engine), _RaisingSummarizer())

        with pytest.raises(RuntimeError, match="did not answer"):
            await stage.run("alpha")

        pool = engine.pool
        assert isinstance(pool, QueuePool)
        assert pool.checkedout() == 0
    finally:
        await engine.dispose()
