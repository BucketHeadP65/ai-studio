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

It runs on the session's testcontainer (`migrated`, a fresh database migrated to head) and only on the
default `pg_textsearch` bed: under `KNOWLEDGE_BASE_TEST_DIALECT=pg_search` the module skips, since that bed
builds the ParadeDB body instead. It never connects to the stack's own database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest

from engram.testing.infrastructure.knowledge_base_db import BED_BM25_EXTENSION
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# ── seed knowledge base ────────────────────────────────────────────────────────────────
# Three `card`-level pages whose card_text carries one distinctive content term each, plus shared
# filler. `_PRESENT_TERM` appears in exactly one card; `_ABSENT_TERM` appears in none. The
# fixture seeds these into a fresh database migrated under `pg_textsearch`, so the BM25 legs have a real
# knowledge base to rank, and a body that matches nothing returns [] for the present term and fails the guard.
# `_PRESENT_TERM` is an invented token existing only in the first seeded card.
_SEARCH = "SELECT * FROM api.search($1, k => $2, collection => 'research')"

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
    pool retains them, all in the `research` collection the search reads.
    """
    await conn.executemany(
        "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash)"
        " VALUES ('research', $1, 'entity', $2, $3, 'verified', $4, $5)",
        [(card_id, slug, title, card_text, f"h-{card_id}") for card_id, slug, title, card_text in _CARDS],
    )


@pytest.fixture
async def search_conn(migrated: str) -> AsyncIterator[asyncpg.Connection]:
    """An asyncpg connection to a fresh database of the session's testcontainer, migrated to head and seeded."""
    if BED_BM25_EXTENSION != "pg_textsearch":
        pytest.skip("the pg_textsearch body is built on the pg_textsearch bed only")
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await _seed_cards(connection)
        yield connection
    finally:
        await connection.close()


class TestBm25Search:
    """`api.search` under `pg_textsearch` returns BM25 hits on a seeded knowledge base (the bug's guard)."""

    async def test_present_term_returns_at_least_one_hit(self, search_conn: asyncpg.Connection):
        # The regression: a present term must match its card's BM25 leg. The buggy body returned []
        # here (the bare `id` bound to the NULL OUT var, emptying the pool) even though the card matches.
        rows = await search_conn.fetch(_SEARCH, _PRESENT_TERM, 12)

        assert len(rows) >= 1, f"expected >=1 BM25 hit for the seeded term {_PRESENT_TERM!r}, got {len(rows)}"

    async def test_present_term_surfaces_the_matching_card(self, search_conn: asyncpg.Connection):
        rows = await search_conn.fetch(_SEARCH, _PRESENT_TERM, 12)

        # The card whose card_text carries the term must be among the returned ids (the BM25 leg
        # ranks it), not merely "some row came back".
        assert "ent.bm25smoke-ann" in {row["id"] for row in rows}

    async def test_absent_term_returns_no_hits(self, search_conn: asyncpg.Connection):
        # A term in no card matches no BM25 leg (and there is no query_vec, so the vector legs are
        # empty too) — the fused result set is empty. This pins that the fix did not "match everything".
        rows = await search_conn.fetch(_SEARCH, _ABSENT_TERM, 12)

        assert rows == []
