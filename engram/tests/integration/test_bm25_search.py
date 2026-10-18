r"""Functional `api.search` BM25 retrieval under the `pg_textsearch` dialect.

Where :class:`tests.integration.test_migrations.TestDialectProfiles` proves the dialect-routed
chain *creates* a plannable `api.search` on each backend (its smoke call asserts an empty knowledge base
returns `[]`), this module proves the `pg_textsearch` body **returns the right rows on a seeded
knowledge base**: a handful of `kb.pages` cards with known text are inserted, then `api.search` is
called for a term present in a card (must return >=1 hit) and for a term absent from every card
(must return 0). Two constraints the body must hold or every BM25 leg silently matches nothing:
under `#variable_conflict use_variable` every `id` reference must be relation-qualified, or a bare
`id` binds to the NULL RETURNS-TABLE OUT variable rather than a column and empties each leg's page
pool; and `<@>` is rank-only — it scores non-matches `0.0` instead of filtering, so each leg needs
its own `col <@> to_bm25query(...) < 0` match filter. The present-term hit pins the first, the
absent-term zero the second.

It is gated on a reachable **local Postgres** (the `pg_textsearch` + AGE Postgres behind
docker-compose's `postgres` service, port 5434), mirroring the skip-gating + async-migration
fixture pattern of :mod:`tests.integration.test_graph_retrieval`'s ``age`` profile and
:func:`tests.integration.test_migrations._migrated_pg_textsearch`. A passing run here proves the body against `pg_textsearch`. The whole module `pytest.skip`\\ s when the analog's admin URL
(:data:`_ANALOG_URL_ENV`, defaulted to the compose profile's URL) is unreachable, so a normal
Docker-only / Docker-less run stays green. `asyncio_mode = auto` runs the `async def` tests
without a decorator; each opens its own asyncpg connection.
"""

from __future__ import annotations

import os

from typing import TYPE_CHECKING

import asyncpg
import pytest

from engram.testing.infrastructure.knowledge_base_db import run_upgrade_head
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# The admin (SQLAlchemy `+asyncpg`) URL of the local Postgres. Defaults to the
# docker-compose `postgres` service's published mapping (host port 5434, memory/memory/memory),
# overridable via env so CI / a remote analog can point elsewhere. The module skips when this URL
# is unreachable, the BM25 analogue of test_migrations' `WIKI_PG_TEXTSEARCH_URL` gate.
_ANALOG_URL_ENV = "WIKI_LOCAL_POSTGRES_URL"
_DEFAULT_ANALOG_URL = "postgresql+asyncpg://memory:memory@localhost:5434/memory"

# The env seam alembic/env.py reads to pick the BM25 dialect (APP_RETRIEVAL__DIALECT override /
# retrieval.dialect); `pg_textsearch` routes 0001's BM25 `CREATE EXTENSION` + index DDL + `api.search` to `pg_textsearch`.
_DB_DIALECT_ENV = "APP_RETRIEVAL__DIALECT"

# ── seed knowledge base ────────────────────────────────────────────────────────────────
# Three `card`-level pages whose card_text carries one distinctive content term each, plus shared
# filler. `_PRESENT_TERM` appears in exactly one card; `_ABSENT_TERM` appears in none. The
# fixture migrates the analog under `pg_textsearch` and seeds these, so the BM25 legs have a real
# knowledge base to rank — a body that matches nothing returns [] for the present term and fails the guard.
#
# The analog is persistent and may hold a REAL indexed knowledge base (it is the default local dev DB), so
# every seeded id/slug MUST carry the `bm25smoke` namespace: a generic id can collide with a
# indexer-produced page, and `_clean_cards` would then delete live rows. `_PRESENT_TERM` is an
# invented token existing only in the first seeded card, so the surfaces-the-card assertion cannot
# be crowded out by real-knowledge_base hits.
_PRESENT_TERM = "flurbwikann"
_ABSENT_TERM = "zzqxwvunobtainium"

# (id, slug, title, card_text). card_text is the column the `pages_bm25` `<@>` leg matches.
_CARDS: tuple[tuple[str, str, str, str], ...] = (
    (
        "ent.bm25smoke-ann",
        "bm25smoke-ann",
        "Approximate Nearest Neighbour",
        "Flurbwikann delivers advanced filtering for billion-scale vector search.",
    ),
    ("ent.bm25smoke-lex", "bm25smoke-lex", "Lexical Retrieval", "BM25 is a sparse lexical ranking function over an inverted index."),
    (
        "ent.bm25smoke-rrf",
        "bm25smoke-rrf",
        "Reciprocal Rank Fusion",
        "Reciprocal rank fusion merges ranked result lists into one hybrid ranking.",
    ),
)


async def _seed_cards(conn: asyncpg.Connection) -> None:
    """Insert the `card`-level `kb.pages` rows the BM25 legs rank over.

    Direct asyncpg inserts (the idiom of :mod:`tests.integration.test_graph_retrieval`): every NOT
    NULL `kb.pages` column (page_type/slug/title/status/card_text/body_hash) is supplied; `status`
    is `verified` (not `stub`) and `superseded_by` defaults NULL so the `only_current` page
    pool retains them, and `collection` defaults to `research` so the default facet keeps them.
    """
    await conn.executemany(
        "INSERT INTO kb.pages (id, page_type, slug, title, status, card_text, body_hash) VALUES ($1, 'entity', $2, $3, 'verified', $4, $5)",
        [(card_id, slug, title, card_text, f"h-{card_id}") for card_id, slug, title, card_text in _CARDS],
    )


async def _clean_cards(conn: asyncpg.Connection) -> None:
    """Remove the seeded cards so a persistent analog asserts over a pristine knowledge_base.

    The analog (unlike the per-test container database) is external and persistent across runs, so a
    leftover seed from an aborted run would skew BM25 ranking — delete by the seeded ids on both setup
    and teardown.
    """
    await conn.execute("DELETE FROM kb.pages WHERE id = ANY($1)", [card_id for card_id, *_rest in _CARDS])


async def _migrate_analog(admin_url: str) -> None:
    """Run `alembic upgrade head` on the analog with `APP_RETRIEVAL__DIALECT=pg_textsearch`.

    Sets the dialect seam `alembic/env.py` reads for the duration of the upgrade (restored after),
    so 0001's BM25 `CREATE EXTENSION` + index DDL and 0002's `api.search` resolve to
    `pg_textsearch` — the local Postgres ships `pg_textsearch` (+ AGE), not ParadeDB's `pg_search`,
    exactly as :func:`tests.integration.test_migrations._migrated_pg_textsearch` does for a gated
    `pg_textsearch` database. The local Postgres is persistent, so `upgrade head` is a no-op when already at head.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(_DB_DIALECT_ENV, "pg_textsearch")
        run_upgrade_head(admin_url)


async def _analog_reachable(admin_url: str) -> bool:
    """Whether the analog accepts an asyncpg connection at `admin_url` (gates the module)."""
    try:
        connection = await asyncpg.connect(_asyncpg_dsn(admin_url))
    except (OSError, asyncpg.PostgresError):
        return False
    await connection.close()
    return True


@pytest.fixture
async def search_conn() -> AsyncIterator[asyncpg.Connection]:
    r"""An asyncpg connection to the migrated + freshly-seeded analog under the `pg_textsearch` dialect.

    Resolves the analog admin URL from :data:`_ANALOG_URL_ENV` (defaulting to the compose profile's
    mapping), `pytest.skip`\\ s the whole module when it is unreachable (so Docker-only / Docker-less
    runs skip cleanly), migrates it under `APP_RETRIEVAL__DIALECT=pg_textsearch` and hands back a connection over
    a clean, seeded knowledge base — cleaned again on teardown so the persistent analog stays pristine.
    """
    admin_url = os.environ.get(_ANALOG_URL_ENV, _DEFAULT_ANALOG_URL)
    if not await _analog_reachable(admin_url):
        pytest.skip(f"local Postgres unreachable at {admin_url}. Start it with `docker compose up -d postgres`")

    await _migrate_analog(admin_url)
    connection = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        await _clean_cards(connection)
        await _seed_cards(connection)
        yield connection
    finally:
        await _clean_cards(connection)
        await connection.close()


class TestBm25Search:
    """`api.search` under `pg_textsearch` returns BM25 hits on a seeded knowledge base (the bug's guard)."""

    async def test_present_term_returns_at_least_one_hit(self, search_conn: asyncpg.Connection):
        # The regression: a present term must match its card's BM25 leg. The buggy body returned []
        # here (the bare `id` bound to the NULL OUT var, emptying the pool) even though the card matches.
        rows = await search_conn.fetch("SELECT * FROM api.search($1, $2)", _PRESENT_TERM, 12)

        assert len(rows) >= 1, f"expected >=1 BM25 hit for the seeded term {_PRESENT_TERM!r}, got {len(rows)}"

    async def test_present_term_surfaces_the_matching_card(self, search_conn: asyncpg.Connection):
        rows = await search_conn.fetch("SELECT * FROM api.search($1, $2)", _PRESENT_TERM, 12)

        # The card whose card_text carries the term must be among the returned ids (the BM25 leg
        # ranks it), not merely "some row came back".
        assert "ent.bm25smoke-ann" in {row["id"] for row in rows}

    async def test_absent_term_returns_no_hits(self, search_conn: asyncpg.Connection):
        # A term in no card matches no BM25 leg (and there is no query_vec, so the vector legs are
        # empty too) — the fused result set is empty. This pins that the fix did not "match everything".
        rows = await search_conn.fetch("SELECT * FROM api.search($1, $2)", _ABSENT_TERM, 12)

        assert rows == []
