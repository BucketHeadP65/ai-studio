"""Open the agent-memory store over Postgres: the vendor store holds the notes, engram indexes them as pages.

`embedded_memory_store` opens one writer pool for the vendor `PostgresMemoryStore` and engram's own write path over
the same writer role. `_CardRows` and `_SearchHits` answer the two reads the store asks for, over the same pool.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.db import open_asyncpg_pool
from pydantic_ai_harness.memory import PostgresMemoryStore

from engram.agent_memory.domain import page_id_of
from engram.agent_memory.infrastructure.schema import agent_memory_schema
from engram.agent_memory.service import EngramMemoryStore
from engram.retrieval.contracts import embedded_authoring, halfvec_literal

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    import asyncpg

    from core.ai.embedding import BatchEmbedder, QueryEmbedder
    from pydantic_ai_harness.memory import PostgresPool

# The vendor store's SQL is unqualified. A schema named like the connecting role would shadow `public` without this.
_SEARCH_PATH = "public"


class _CardRows:
    """Read the live cards of one collection over a connection pool."""

    def __init__(self, pool: asyncpg.Pool, *, collection: str) -> None:
        self._pool = pool
        self._collection = collection

    async def __call__(self) -> Sequence[tuple[str, str, str]]:
        """Return `(page_id, title, summary)` for every live page in this collection that nothing supersedes."""
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT id, title, COALESCE(summary, '') AS summary FROM api.cards "
                "WHERE src_collection = $1 AND status = 'live' AND NOT superseded",
                self._collection,
            )
        return [(row["id"], row["title"], row["summary"]) for row in rows]


# `$4` (the collection) is bound twice, once to scope `api.search` and once to scope the card join.
_SEARCH_SQL = """
with hits as (
    select page_id, max(score) as score
    from api.search($1, $2, cast($3 as text)::halfvec, collection => $4)
    group by page_id
)
select c.id, coalesce(c.summary, '') as summary, h.score
from hits h
join api.cards c on c.id = h.page_id
where c.src_collection = $4
  and c.status = 'live'
  and not c.superseded
  and starts_with(c.id, $5)
order by h.score desc, c.id
limit $6
"""


def _search_k(limit: int) -> int:
    """Return the `k` for `api.search`, which caps it at 50."""
    return min(50, limit * 5)


class _SearchHits:
    """Rank live pages of one collection under an id prefix, best score first."""

    def __init__(self, pool: asyncpg.Pool, embed_query: QueryEmbedder, *, collection: str) -> None:
        self._pool = pool
        self._embed_query = embed_query
        self._collection = collection

    async def __call__(self, query: str, *, prefix: str, limit: int) -> Sequence[tuple[str, str, float]]:
        """Return `(page_id, snippet, score)` best first, filtered to the id prefix inside the query before its `LIMIT`."""
        vector = halfvec_literal(await self._embed_query(query))
        id_prefix = page_id_of(prefix)
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                _SEARCH_SQL, query, _search_k(limit), vector, self._collection, id_prefix, limit
            )
        return [(row["id"], row["summary"], float(row["score"])) for row in rows]


def _asyncpg_dsn(url: str) -> str:
    """Respell a SQLAlchemy DSN for asyncpg, turning its `ssl` query key into `sslmode`."""
    plain = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parts = urlsplit(plain)
    if not parts.query:
        return plain
    query = [("sslmode", value) if key == "ssl" else (key, value) for key, value in parse_qsl(parts.query)]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _open_pool(writer_url: str, *, min_size: int, max_size: int) -> asyncpg.Pool:
    """Open a writer pool over `writer_url` with `search_path` pinned to `public`."""
    return await open_asyncpg_pool(
        _asyncpg_dsn(writer_url),
        min_size=min_size,
        max_size=max_size,
        server_settings={"search_path": _SEARCH_PATH},
        target=f"for the agent-memory store at {writer_url!r}",
    )


@asynccontextmanager
async def embedded_memory_store(
    writer_url: str,
    *,
    embedder: BatchEmbedder,
    embed_query: QueryEmbedder | None = None,
    collection: str = "agent-memory",
    pool_min_size: int = 1,
    pool_max_size: int = 2,
) -> AsyncGenerator[EngramMemoryStore]:
    """Open the agent-memory store: the vendor Postgres store holds the notes, engram indexes each one as a page.

    Both run over one writer role. Leaving `embed_query` as `None` keeps the store working, and `search` then uses
    the vendor store's own lexical search.

    Args:
        writer_url: The DSN for a writer role, in any spelling `core.db.async_dsn` accepts.
        embedder: Embeds every indexed note (`engram.document_embedder` builds one from an embedding contract).
        embed_query: Embeds a search query into the same vector space. Passing one turns on ranked search.
        collection: The collection notes are indexed into and cards and search read from.
        pool_min_size: The fewest connections the writer pool keeps open.
        pool_max_size: The most connections the writer pool opens.

    Yields:
        The `EngramMemoryStore`. The pool and the ingest engine are both closed when the context exits.
    """
    pool = await _open_pool(writer_url, min_size=pool_min_size, max_size=pool_max_size)
    try:
        # `asyncpg.Pool` has every method `PostgresPool` names. Its extra optional `timeout` keyword is never passed.
        pool_as_postgres_pool = cast("PostgresPool", pool)
        async with embedded_authoring(writer_url, spec=agent_memory_schema(), embedder=embedder) as ingest:
            inner = PostgresMemoryStore(pool_as_postgres_pool)
            cards = _CardRows(pool, collection=collection)
            search_hits = _SearchHits(pool, embed_query, collection=collection) if embed_query is not None else None
            yield EngramMemoryStore(inner, ingest, cards, collection=collection, search_hits=search_hits)
    finally:
        await pool.close()
