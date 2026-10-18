"""A re-ranked search reads its hits and their texts in one snapshot, and holds no transaction through the model call.

One shared collection holds two cards that match the query. The search runs as `kb_reader` inside `read_snapshot()`, the
way a tool call runs it, with a decision model that answers after one second. While that model call is in flight, the
owner counts the reader's sessions that sit inside an open transaction.

What this pins:

* the hits and their whole texts were read before the model call;
* no reader session is inside a transaction while the model call is in flight.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import asyncpg
import pytest

from core.ai.decision_models import DecisionModelQuestions
from core.testing.fakes import FakeDecisionModel
from pydantic_ai.models import override_allow_model_requests
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.contracts import Reranking
from engram.retrieval.domain import Search
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway, read_snapshot
from engram.retrieval.queries import FetchHandler, SearchHandler
from engram.testing.fakes.embedding import FakeEmbedding
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import Iterator

_COLLECTION = "notes"
_WORD = "heliotrope"
_IN_TRANSACTION = (
    "SELECT count(*) FROM pg_stat_activity WHERE usename = 'kb_reader' AND datname = current_database()"
    " AND state LIKE 'idle in transaction%'"
)


@pytest.fixture(autouse=True)
def _allow_the_scripted_model() -> Iterator[None]:
    with override_allow_model_requests(allow_model_requests=True):
        yield


async def test_no_transaction_stays_open_through_the_model_call(migrated: str, reader_url: str):
    admin = await asyncpg.connect(_asyncpg_dsn(migrated))
    engine = create_async_engine(reader_url, poolclass=NullPool)
    try:
        await admin.executemany(
            "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets)"
            " VALUES ($1, $2, 'entity', $2, $2, 'live', $3, $2, '{}'::jsonb)",
            [(_COLLECTION, "ent.one", f"{_WORD} one"), (_COLLECTION, "ent.two", f"{_WORD} two")],
        )
        gateway = ReadOnlySqlGateway(engine, collection=_COLLECTION)
        model = FakeDecisionModel(delay_seconds=1.0)
        reranked = Reranking(questions=DecisionModelQuestions(model), passage="chunk", deadline_seconds=10.0).over(
            SearchHandler(gateway, FakeEmbedding()), FetchHandler(gateway, _COLLECTION)
        )

        async def search() -> int:
            async with read_snapshot():
                result = await reranked(Search(query=_WORD, k=5, collection=_COLLECTION, hybrid=False, level="card"))
            return len(result.rows)

        running = asyncio.create_task(search())
        while not model.requests:  # noqa: ASYNC110 (the fake model offers no event to wait on, so the test polls for its first request)
            await asyncio.sleep(0.02)
        open_during_the_call = await admin.fetchval(_IN_TRANSACTION)
        hits = await running
    finally:
        await engine.dispose()
        await admin.close()

    assert hits == 2
    assert open_during_the_call == 0
