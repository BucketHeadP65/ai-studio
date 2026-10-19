"""`api.search` keeps one hit per document and folds the document's other matching units under it.

One shared collection holds six documents whose every unit carries one made-up word:

* `alpha`, a source with one raw chunk and a page made from it, carrying `shade: teal`, with its card, one
  section and one claim.
* `beta`, an entity page made from no source, with its card and one section.
* `gamma` and `delta`, two sources with one raw chunk each and no page.
* `epsilon`, an entity page with its card alone.
* `other`, a source with one raw chunk and a page made from it, carrying `shade: amber`.

Every search runs keyword only, as `kb_reader`, and the owner changes the tuning row.

What this pins:

* each document comes back once, and alpha's hit lists its three other units as folded units, best first.
* `k` counts documents, the first `k` documents of the uncapped ranking.
* an entity page made from no source is a document of its own.
* a cap of 0 or null gives back the ranking of the search before the cap, folding nothing.
* a cap of 2 keeps two hits of a document and folds the rest under its best.
* `facets` still narrow a capped search.
* the tuning row starts with a cap of 1 and refuses a cap that is neither a number nor null.
* the reader reads the cap the search applies, through SQL and through the search handler, and with no tuning row
  both the reader and the search use the default cap of 1.
* the comment an agent reads names the cap and the folded units, and one step down puts 0069's search, row, check,
  comment and functions back.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import asyncpg
import pytest

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.contracts import read_hits_per_document
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.queries import SearchHandler
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.infrastructure.knowledge_base_db import run_downgrade, run_upgrade_head
from tests.integration.test_migrations import _asyncpg_dsn, _load_migration_module

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_COLLECTION = "notes"
_WORD = "quillwort"
_SEARCH = "SELECT * FROM api.search($1, k => $2, collection => $3, facets => $4::jsonb)"
_COMMENT = (
    "SELECT obj_description(p.oid, 'pg_proc') FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE n.nspname = 'api' AND p.proname = 'search'"
)

_DOCUMENT_OF = {
    "note.alpha": "alpha",
    "note.alpha#s0": "alpha",
    "note.alpha#c0": "alpha",
    "src.alpha:0": "alpha",
    "ent.beta": "beta",
    "ent.beta#s0": "beta",
    "src.gamma:0": "gamma",
    "src.delta:0": "delta",
    "ent.epsilon": "epsilon",
    "note.other": "other",
    "src.other:0": "other",
}
"""Every seeded unit's document."""


async def _source(connection: asyncpg.Connection, source_id: str) -> None:
    """One source mirror with one raw chunk carrying the word."""
    await connection.execute(
        "INSERT INTO kb.sources_raw (collection, source_id, title, source_hash, provider, provider_key, meta)"
        " VALUES ($1, $2, $2, 'h', 'intake', $2, '{}'::jsonb)",
        _COLLECTION,
        source_id,
    )
    await connection.execute(
        "INSERT INTO kb.raw_chunks (collection, id, source_id, seq, section_path, heading, body)"
        " VALUES ($1, $2, $3, 0, 'Body'::ltree, 'Body', $4)",
        _COLLECTION,
        f"{source_id}:0",
        source_id,
        f"{_WORD} in the chunk of {source_id}",
    )


async def _page(connection: asyncpg.Connection, page_id: str, *, source_id: str | None = None, shade: str | None = None) -> None:
    """One live page whose card carries the word, made from `source_id` and carrying `shade` when given."""
    await connection.execute(
        "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets, source_id)"
        " VALUES ($1, $2, 'note', $2, $2, 'live', $3, $2, $4::jsonb, $5)",
        _COLLECTION,
        page_id,
        f"{_WORD} on the card of {page_id}",
        json.dumps({"shade": [shade]} if shade else {}),
        source_id,
    )


async def _section(connection: asyncpg.Connection, page_id: str) -> None:
    """One section of `page_id` carrying the word."""
    await connection.execute(
        "INSERT INTO kb.sections (collection, id, page_id, ord, canon, path, heading, body, body_hash, tokens)"
        " VALUES ($1, $2, $3, 0, 'definition', 'Definition'::ltree, 'Definition', $4, 'h', 3)",
        _COLLECTION,
        f"{page_id}#s0",
        page_id,
        f"{_WORD} in a section of {page_id}",
    )


async def _claim(connection: asyncpg.Connection, page_id: str) -> None:
    """One active claim of `page_id` carrying the word."""
    await connection.execute(
        "INSERT INTO kb.claims (collection, id, page_id, block_id, kind, text, status, subject_collection, subject_id)"
        " VALUES ($1, $2, $3, $2, 'support', $4, 'active', $1, $3)",
        _COLLECTION,
        f"{page_id}#c0",
        page_id,
        f"{_WORD} in a claim of {page_id}",
    )


@pytest.fixture
async def admin(migrated: str) -> AsyncIterator[asyncpg.Connection]:
    """An owner connection over the six documents."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        for source_id in ("src.alpha", "src.gamma", "src.delta", "src.other"):
            await _source(connection, source_id)
        await _page(connection, "note.alpha", source_id="src.alpha", shade="teal")
        await _section(connection, "note.alpha")
        await _claim(connection, "note.alpha")
        await _page(connection, "ent.beta")
        await _section(connection, "ent.beta")
        await _page(connection, "ent.epsilon")
        await _page(connection, "note.other", source_id="src.other", shade="amber")
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def reader(admin: asyncpg.Connection, reader_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A `kb_reader` connection over the same database."""
    del admin
    connection = await asyncpg.connect(_asyncpg_dsn(reader_url))
    try:
        yield connection
    finally:
        await connection.close()


async def _search(connection: asyncpg.Connection, k: int = 20, facets: dict[str, list[str]] | None = None) -> list[asyncpg.Record]:
    """The rows one keyword search for the word answers over the collection."""
    return await connection.fetch(_SEARCH, _WORD, k, _COLLECTION, json.dumps(facets or {}))


def _folded(row: asyncpg.Record) -> list[tuple[str, str]]:
    """A row's folded units as `(id, hit_level)` pairs, in the order the row lists them."""
    return [(unit["id"], unit["hit_level"]) for unit in json.loads(row["folded_units"])]


def _documents(rows: list[asyncpg.Record]) -> list[str]:
    """The document of each row, in rank order."""
    return [_DOCUMENT_OF[row["id"]] for row in rows]


async def _cap(admin: asyncpg.Connection, cap: str) -> None:
    """Set the tuning row's cap on hits per document to the jsonb value `cap`, written as JSON text."""
    await admin.execute("UPDATE kb.retrieval_tuning SET tuning = tuning || jsonb_build_object('max_hits_per_document', $1::jsonb) WHERE id = 1", cap)


async def test_each_document_comes_back_once(reader: asyncpg.Connection):
    rows = await _search(reader)

    assert sorted(_documents(rows)) == ["alpha", "beta", "delta", "epsilon", "gamma", "other"]


async def test_a_documents_card_section_claim_and_chunk_come_back_as_one_hit_with_the_rest_folded(reader: asyncpg.Connection):
    rows = await _search(reader)
    (alpha,) = [row for row in rows if _DOCUMENT_OF[row["id"]] == "alpha"]

    assert {(alpha["id"], alpha["hit_level"]), *_folded(alpha)} == {
        ("note.alpha", "card"),
        ("note.alpha#s0", "section"),
        ("note.alpha#c0", "claim"),
        ("src.alpha:0", "raw"),
    }
    assert len(_folded(alpha)) == 3


async def test_the_best_unit_stands_for_its_document_and_the_next_ones_fold_best_first(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _cap(admin, "0")
    uncapped = [row["id"] for row in await _search(reader) if _DOCUMENT_OF[row["id"]] == "alpha"]
    await _cap(admin, "1")

    (alpha,) = [row for row in await _search(reader) if _DOCUMENT_OF[row["id"]] == "alpha"]

    assert [alpha["id"], *(unit_id for unit_id, _level in _folded(alpha))] == uncapped


async def test_k_counts_documents(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _cap(admin, "0")
    first_seen = list(dict.fromkeys(_documents(await _search(reader))))
    await _cap(admin, "1")

    rows = await _search(reader, k=3)

    assert _documents(rows) == first_seen[:3]


async def test_an_entity_page_made_from_no_source_is_a_document_of_its_own(reader: asyncpg.Connection):
    rows = await _search(reader)
    (beta,) = [row for row in rows if _DOCUMENT_OF[row["id"]] == "beta"]

    assert {beta["id"], *(unit_id for unit_id, _level in _folded(beta))} == {"ent.beta", "ent.beta#s0"}
    assert [row["id"] for row in rows if row["id"] == "ent.epsilon"] == ["ent.epsilon"]


async def test_a_cap_of_zero_gives_back_the_ranking_before_the_cap(migrated: str, admin: asyncpg.Connection, reader: asyncpg.Connection):
    run_downgrade(migrated, "0069")
    before = [(row["id"], row["hit_level"]) for row in await _search(reader)]
    run_upgrade_head(migrated)
    await _cap(admin, "0")

    rows = await _search(reader)

    assert [(row["id"], row["hit_level"]) for row in rows] == before
    assert all(_folded(row) == [] for row in rows)


async def test_a_null_cap_keeps_every_hit_like_a_cap_of_zero(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _cap(admin, "0")
    zero = [row["id"] for row in await _search(reader)]
    await _cap(admin, "null")

    rows = await _search(reader)

    assert [row["id"] for row in rows] == zero
    assert len(rows) == len(_DOCUMENT_OF)


async def test_a_cap_of_two_keeps_two_hits_of_a_document_and_folds_the_rest_under_its_best(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _cap(admin, "2")

    best, second = [row for row in await _search(reader) if _DOCUMENT_OF[row["id"]] == "alpha"]

    assert len(_folded(best)) == 2
    assert _folded(second) == []


async def test_facets_still_narrow_a_capped_search(reader: asyncpg.Connection):
    rows = await _search(reader, facets={"shade": ["teal"]})

    assert _documents(rows) == ["alpha"]
    assert len(_folded(rows[0])) == 3


async def test_the_tuning_row_starts_with_a_cap_of_one_and_takes_a_number_or_null(admin: asyncpg.Connection):
    tuning = json.loads(str(await admin.fetchval("SELECT tuning FROM kb.retrieval_tuning WHERE id = 1")))
    await _cap(admin, "null")

    with pytest.raises(asyncpg.CheckViolationError):
        await _cap(admin, '"one"')

    assert tuning["max_hits_per_document"] == 1


@pytest.mark.parametrize(("cap", "read"), [("1", 1), ("0", 0), ("null", 0), ("2", 2), ("99", 50)])
async def test_the_reader_reads_the_cap_the_search_applies(admin: asyncpg.Connection, reader: asyncpg.Connection, cap: str, read: int):
    await _cap(admin, cap)

    assert await reader.fetchval("SELECT api.search_hits_per_document()") == read


async def test_with_no_tuning_row_the_reader_and_the_search_both_use_the_default_cap_of_one(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await admin.execute("DELETE FROM kb.retrieval_tuning")

    cap = await reader.fetchval("SELECT api.search_hits_per_document()")
    rows = await _search(reader)

    assert cap == 1
    assert sorted(_documents(rows)) == ["alpha", "beta", "delta", "epsilon", "gamma", "other"]


async def test_the_search_handler_reads_the_cap_through_the_reader_gateway(admin: asyncpg.Connection, reader_url: str):
    await _cap(admin, "2")
    engine = create_async_engine(reader_url, poolclass=NullPool)
    try:
        search = SearchHandler(ReadOnlySqlGateway(engine, collection=_COLLECTION), FakeEmbedding())
        direct = await search.hits_per_document()
        safe = await read_hits_per_document(search, knowledge_base=_COLLECTION)
    finally:
        await engine.dispose()

    assert (direct, safe) == (2, 2)


async def _comment(url: str) -> str:
    """The comment on `api.search`."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return str(await connection.fetchval(_COMMENT))
    finally:
        await connection.close()


async def test_the_comment_names_the_cap_and_the_folded_units(migrated: str):
    comment = await _comment(migrated)

    assert "each document keeps its best hit" in comment
    assert "max_hits_per_document" in comment
    assert "folded_units" in comment


async def test_one_step_down_puts_the_previous_search_row_and_comment_back(migrated: str, admin: asyncpg.Connection, reader: asyncpg.Connection):
    run_downgrade(migrated, "0069")
    columns = [attribute.name for attribute in (await reader.prepare(_SEARCH)).get_attributes()]
    tuning = json.loads(str(await admin.fetchval("SELECT tuning FROM kb.retrieval_tuning WHERE id = 1")))
    below = await _comment(migrated)
    reader_function = await admin.fetchval("SELECT to_regprocedure('api.search_hits_per_document()')")
    run_upgrade_head(migrated)

    assert "folded_units" not in columns
    assert "max_hits_per_document" not in tuning
    assert below == _load_migration_module("0066")._COMMENT_TEXT
    assert reader_function is None


_CHECK = (
    "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
    " WHERE t.relname = 'retrieval_tuning' AND c.conname = 'retrieval_tuning_numbers'"
)


async def test_one_step_down_puts_back_the_check_that_knows_no_cap(migrated: str, admin: asyncpg.Connection):
    run_downgrade(migrated, "0069")
    check = str(await admin.fetchval(_CHECK))
    await admin.execute("""UPDATE kb.retrieval_tuning SET tuning = tuning || '{"max_hits_per_document": "one"}'::jsonb WHERE id = 1""")
    with pytest.raises(asyncpg.CheckViolationError):
        await admin.execute("""UPDATE kb.retrieval_tuning SET tuning = tuning || '{"rows_per_group": "three"}'::jsonb WHERE id = 1""")
    await admin.execute("UPDATE kb.retrieval_tuning SET tuning = tuning - 'max_hits_per_document' WHERE id = 1")
    run_upgrade_head(migrated)

    assert "rows_per_group" in check
    assert "max_hits_per_document" not in check
