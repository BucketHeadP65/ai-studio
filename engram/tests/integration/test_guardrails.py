r"""The `kb_reader` guardrails, against a real migrated ParadeDB — Docker-gated.

The read-only floor enforced by migration 0003's role + the function bodies in 0002, proven
against a live database:

* the `kb_reader` role may **read the api surface only** — direct `INSERT` / `UPDATE` on
  `kb.*` is rejected (read-only transaction / permission), and even `SELECT` on `kb.*`
  is denied (only `api` is granted), while `SELECT` on `api.cards` works;
* `api.log_session` is the role's single permitted write — a `SECURITY DEFINER` function that
  lands a row in `kb.query_log` (verified through an admin engine) without any table grant.
  `SECURITY DEFINER` changes the *user*, not the transaction's mode, so under the role's
  `default_transaction_read_only = on` default the call is blocked unless the telemetry path
  first opts the transaction into read-write (`SET TRANSACTION READ WRITE`); both the blocked
  and the opted-in paths are pinned here;
* `api.search` hard-caps `k` at 50 (`k => 999` returns at most 50 rows);
* `api.fetch` truncates a page body past the ~8k-token budget with the contract's exact marker.

Seeding is direct admin DML (these tests exercise the role + function guardrails, not the index
path), so they stay fast. The whole module skips when Docker is unavailable (the `pg` fixture,
via `migrated`, raises `pytest.skip`). Tests are *synchronous*, wrapping their async DB work in
`asyncio.run` — the shape `tests/e2e/test_compile_and_search.py` uses.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from engram.indexing.infrastructure import orm
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from engram.retrieval.domain import SqlResult

# api.fetch truncates page bodies past ~8k tokens (~32k chars); seed one comfortably over it.
_BIG_BODY_CHARS = 40000
_TRUNCATION_MARKER = "[truncated — fetch sections instead]"


def _run[T](coro: Coroutine[object, object, T]) -> T:
    """Drive a coroutine from a synchronous test body (no loop is running)."""
    return asyncio.run(coro)


async def _seed(admin_url: str) -> None:
    """Seed (as admin) a normal page, a long page, and 60 keyword-matching sections."""
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash) "
                    "VALUES ('paper.guard', 'research', 'paper', 'guard', 'Guardrail Paper', 'synthesized', 'A small page.', "
                    "'paper: Guardrail Paper — a small page.', 'h1')"
                )
            )
            # A page whose card_text exceeds the fetch char budget → api.fetch must truncate it.
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash) "
                    "VALUES ('paper.long', 'research', 'paper', 'long', 'Long Paper', 'synthesized', :big, 'h2')"
                ),
                {"big": "lorem ipsum " * (_BIG_BODY_CHARS // 12)},
            )
            # 60 sections all matching the keyword "alpha" → api.search(k => 999) must cap at 50.
            for i in range(60):
                await conn.execute(
                    text(
                        "INSERT INTO kb.sections (id, collection, page_id, heading, path, ord, body, body_hash) "
                        "VALUES (:id, 'research', 'paper.guard', :h, :p, :ord, 'alpha capping probe section', :bh)"
                    ),
                    {"id": f"paper.guard#s{i}", "h": f"S{i}", "p": f"s{i}", "ord": i, "bh": f"hs{i}"},
                )
            # The 60 sections share one page, so lift the per-page cap past k and the cap per document off: the k clamp alone limits the rows.
            await conn.execute(
                text("""UPDATE kb.retrieval_tuning SET tuning = tuning || '{"rows_per_group": 50, "max_hits_per_document": 0}'::jsonb WHERE collection IS NULL""")
            )
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(migrated: str) -> str:
    """Seed the admin DB; return the admin URL (reader reads go through `reader_url`)."""
    _run(_seed(migrated))
    return migrated


@pytest.fixture
def reader(seeded: str, reader_url: str) -> str:
    """The reader URL over the seeded DB (forces seeding before any reader read)."""
    del seeded  # ordering dependency
    return reader_url


def _reader_rows(reader_url: str, sql: str, params: dict[str, object] | None = None, *, cap: int = 50) -> SqlResult:
    """Run one read through the `kb_reader` gateway."""

    async def _go() -> SqlResult:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            return await ReadOnlySqlGateway(engine).rows(sql, params or {}, cap=cap)
        finally:
            await engine.dispose()

    return _run(_go())


def _reader_execute(reader_url: str, sql: str) -> None:
    """Execute a statement as the reader role inside a transaction (used to prove rejections)."""

    async def _go() -> None:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        finally:
            await engine.dispose()

    _run(_go())


def _reader_log_session(reader_url: str, sql: str) -> None:
    """Call `api.log_session` as the reader in a read-write transaction.

    The role defaults to `default_transaction_read_only = on` (the read floor that protects every
    read path), so the telemetry write must explicitly opt the transaction into read-write —
    `SET TRANSACTION READ WRITE` as its first statement — before invoking the `SECURITY DEFINER`
    `log_session`. This mirrors how the serving layer's telemetry path must call it; the guardrail
    proven here is that the function then writes to `kb.query_log` despite the reader holding no
    table grant on it.
    """

    async def _go() -> None:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SET TRANSACTION READ WRITE"))
                await conn.execute(text(sql))
                await conn.commit()
        finally:
            await engine.dispose()

    _run(_go())


def _admin_scalar(admin_url: str, sql: str) -> object:
    """Read a single value through the admin engine (to verify reader-side writes landed)."""

    async def _go() -> object:
        engine = create_async_engine(admin_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.scalar(text(sql))
        finally:
            await engine.dispose()

    return _run(_go())


class TestReaderIsReadOnlyOverApiOnly:
    def test_insert_into_memory_pages_is_rejected(self, reader: str):
        with pytest.raises(DBAPIError) as excinfo:
            _reader_execute(
                reader,
                "INSERT INTO kb.pages (id, page_type, slug, title, status, card_text, body_hash) "
                "VALUES ('paper.evil', 'paper', 'evil', 'Evil', 'synthesized', 'x', 'h')",
            )
        # Either the read-only transaction or the missing kb.* grant rejects it.
        assert _is_guardrail_error(excinfo.value)

    def test_update_memory_pages_is_rejected(self, reader: str):
        with pytest.raises(DBAPIError) as excinfo:
            _reader_execute(reader, "UPDATE kb.pages SET title = 'hacked' WHERE id = 'paper.guard'")

        assert _is_guardrail_error(excinfo.value)

    def test_select_on_memory_pages_is_denied(self, reader: str):
        # Only the api schema is granted to kb_reader — kb.* is invisible even for reads.
        with pytest.raises(DBAPIError) as excinfo:
            _reader_rows(reader, "SELECT id FROM kb.pages")

        assert "permission denied" in str(excinfo.value).lower()

    def test_select_on_api_cards_works(self, reader: str):
        result = _reader_rows(reader, "SELECT id, title FROM api.cards ORDER BY id")

        assert "paper.guard" in {str(row["id"]) for row in result.rows}


class TestLogSessionIsTheOnlyWrite:
    def test_log_session_insert_lands_a_row(self, reader: str, seeded: str):
        before = int(str(_admin_scalar(seeded, "SELECT count(*) FROM kb.query_log")))

        _reader_log_session(
            reader, "SELECT api.log_session('what is graph rag?', 'compare', ARRAY['api.search'], ARRAY['paper.guard'], 1200, NULL)"
        )

        after = int(str(_admin_scalar(seeded, "SELECT count(*) FROM kb.query_log")))
        question = _admin_scalar(seeded, "SELECT question FROM kb.query_log ORDER BY id DESC LIMIT 1")
        assert after == before + 1  # the SECURITY DEFINER write landed despite no table grant
        assert question == "what is graph rag?"

    def test_log_session_blocked_in_a_read_only_transaction(self, reader: str):
        # Without opting into read-write, the role's default read-only transaction blocks even the
        # SECURITY DEFINER write (SECURITY DEFINER changes the user, not the transaction's mode) —
        # so the telemetry path MUST establish read-write first (see _reader_log_session).
        with pytest.raises(DBAPIError) as excinfo:
            _reader_rows(reader, "SELECT api.log_session('blocked?', 'compare', ARRAY['api.search'], ARRAY['paper.guard'], 1, NULL)")

        assert "read-only" in str(excinfo.value).lower()


class TestSearchKCap:
    def test_k_over_fifty_is_capped_at_fifty(self, reader: str):
        # 60 sections match "alpha"; k => 999 must clamp to the 50-row guardrail floor.
        result = _reader_rows(
            reader,
            "SELECT * FROM api.search(query => :q, k => 999, level => 'section')",
            {"q": "alpha"},
            cap=999,  # let the client cap exceed 50 so the SQL clamp is what limits the rows
        )

        assert len(result.rows) == 50


class TestFetchTruncation:
    def test_long_page_body_is_truncated_with_the_contract_marker(self, reader: str):
        result = _reader_rows(reader, "SELECT * FROM api.fetch('research', 'paper.long')")

        assert len(result.rows) == 1
        body = str(result.rows[0]["body"])
        assert body.endswith(_TRUNCATION_MARKER)
        assert len(body) < _BIG_BODY_CHARS  # the body was actually shortened

    def test_short_page_body_is_returned_whole(self, reader: str):
        result = _reader_rows(reader, "SELECT * FROM api.fetch('research', 'paper.guard')")

        body = str(result.rows[0]["body"])
        assert _TRUNCATION_MARKER not in body
        assert "small page" in body


def _is_guardrail_error(error: BaseException) -> bool:
    """Whether `error` is the read-only-transaction or permission-denied rejection."""
    message = str(error).lower()
    return "read-only" in message or "permission denied" in message
