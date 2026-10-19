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
* the comment an agent reads names the cap and the folded units, and stepping below 0070 puts 0069's search, row,
  check, comment and functions back.
* each folded unit carries its snippet, cut at a whole word to the folded snippet length, or mid-word when no word
  ends inside it.
* the tuning table holds a default row and at most one row per knowledge base, which sets only the cap and the folded
  snippet length and wins over the default row for that knowledge base's hits, also in a search over every knowledge
  base, and the reader reads one knowledge base's cap, its session's own by default.
* 0071 makes the single row the default row, and one step down puts 0070's search, single row, cap function and
  comments back.
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
    """Set the default tuning row's cap on hits per document to the jsonb value `cap`, written as JSON text."""
    await admin.execute(
        "UPDATE kb.retrieval_tuning SET tuning = tuning || jsonb_build_object('max_hits_per_document', $1::jsonb) WHERE collection IS NULL", cap
    )


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
    tuning = json.loads(str(await admin.fetchval("SELECT tuning FROM kb.retrieval_tuning WHERE collection IS NULL")))
    await _cap(admin, "null")

    with pytest.raises(asyncpg.CheckViolationError):
        await _cap(admin, '"one"')

    assert tuning["max_hits_per_document"] == 1


@pytest.mark.parametrize(("cap", "read"), [("1", 1), ("0", 0), ("null", 0), ("2", 2), ("99", 50)])
async def test_the_reader_reads_the_cap_the_search_applies(admin: asyncpg.Connection, reader: asyncpg.Connection, cap: str, read: int):
    await _cap(admin, cap)

    assert await reader.fetchval("SELECT api.search_hits_per_document($1)", _COLLECTION) == read


async def test_with_no_tuning_row_the_reader_and_the_search_both_use_the_default_cap_of_one(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await admin.execute("DELETE FROM kb.retrieval_tuning")

    cap = await reader.fetchval("SELECT api.search_hits_per_document($1)", _COLLECTION)
    rows = await _search(reader)

    assert cap == 1
    assert sorted(_documents(rows)) == ["alpha", "beta", "delta", "epsilon", "gamma", "other"]


async def test_the_search_handler_reads_the_cap_through_the_reader_gateway(admin: asyncpg.Connection, reader_url: str):
    await _cap(admin, "2")
    engine = create_async_engine(reader_url, poolclass=NullPool)
    try:
        search = SearchHandler(ReadOnlySqlGateway(engine, collection=_COLLECTION), FakeEmbedding())
        direct = await search.hits_per_document(_COLLECTION)
        safe = await read_hits_per_document(search, knowledge_base="the notes", collection=_COLLECTION)
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


_OTHER_COLLECTION = "logs"

_TEXT_OF = {
    "note.alpha": f"{_WORD} on the card of note.alpha",
    "note.alpha#s0": f"{_WORD} in a section of note.alpha",
    "note.alpha#c0": f"{_WORD} in a claim of note.alpha",
    "src.alpha:0": f"{_WORD} in the chunk of src.alpha",
}
"""The text each of alpha's units matched."""

_TABLE_COMMENT = "SELECT obj_description('kb.retrieval_tuning'::regclass, 'pg_class')"


def _folded_snippets(row: asyncpg.Record) -> dict[str, str | None]:
    """A row's folded units as id to snippet, `None` for a unit listing no snippet."""
    return {unit["id"]: unit.get("snippet") for unit in json.loads(row["folded_units"])}


def _alpha(rows: list[asyncpg.Record]) -> asyncpg.Record:
    """The one row that stands for alpha."""
    (alpha,) = [row for row in rows if _DOCUMENT_OF[row["id"]] == "alpha"]
    return alpha


async def _default_tuning(admin: asyncpg.Connection, tuning: dict[str, object]) -> None:
    """Merge `tuning` into the default tuning row."""
    await admin.execute("UPDATE kb.retrieval_tuning SET tuning = tuning || $1::jsonb WHERE collection IS NULL", json.dumps(tuning))


async def _knowledge_base_tuning(admin: asyncpg.Connection, collection: str, tuning: dict[str, object]) -> None:
    """Merge `tuning` into `collection`'s own tuning row, adding the row when the knowledge base has none."""
    await admin.execute(
        "INSERT INTO kb.retrieval_tuning (collection, tuning) VALUES ($1, $2::jsonb)"
        " ON CONFLICT (collection) DO UPDATE SET tuning = kb.retrieval_tuning.tuning || EXCLUDED.tuning",
        collection,
        json.dumps(tuning),
    )


async def test_each_folded_unit_carries_the_snippet_of_the_text_it_matched(reader: asyncpg.Connection):
    snippets = _folded_snippets(_alpha(await _search(reader)))

    assert len(snippets) == 3
    assert snippets == {unit_id: _TEXT_OF[unit_id] for unit_id in snippets}


async def test_a_longer_text_is_cut_at_a_whole_word_to_the_folded_snippet_length(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _default_tuning(admin, {"folded_snippet_chars": 20})
    cut = {
        "note.alpha": f"{_WORD} on the…",
        "note.alpha#s0": f"{_WORD} in a…",
        "note.alpha#c0": f"{_WORD} in a…",
        "src.alpha:0": f"{_WORD} in the…",
    }

    snippets = _folded_snippets(_alpha(await _search(reader)))

    assert snippets == {unit_id: cut[unit_id] for unit_id in snippets}


async def test_a_text_with_no_word_end_inside_the_length_is_cut_mid_word(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _default_tuning(admin, {"folded_snippet_chars": 20})
    await admin.execute(
        "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash)"
        " VALUES ($1, 'ent.zeta', 'note', 'ent.zeta', 'ent.zeta', 'live', $2, 'h')",
        _COLLECTION,
        f"{'z' * 30} {_WORD}",
    )
    await admin.execute(
        "INSERT INTO kb.sections (collection, id, page_id, ord, canon, path, heading, body, body_hash, tokens)"
        " VALUES ($1, 'ent.zeta#s0', 'ent.zeta', 0, 'definition', 'Definition'::ltree, 'Definition', $2, 'h', 2)",
        _COLLECTION,
        f"{'y' * 30} {_WORD}",
    )

    (zeta,) = [row for row in await _search(reader) if str(row["id"]).startswith("ent.zeta")]

    assert list(_folded_snippets(zeta).values()) in (["z" * 19 + "…"], ["y" * 19 + "…"])


async def test_a_knowledge_bases_own_cap_wins_over_the_default_cap(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _knowledge_base_tuning(admin, _COLLECTION, {"max_hits_per_document": 2})

    best, second = [row for row in await _search(reader) if _DOCUMENT_OF[row["id"]] == "alpha"]

    assert len(_folded(best)) == 2
    assert _folded(second) == []


async def test_a_knowledge_bases_own_folded_snippet_length_wins_over_the_default(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _knowledge_base_tuning(admin, _COLLECTION, {"folded_snippet_chars": 20})

    snippets = _folded_snippets(_alpha(await _search(reader)))

    assert all(snippet is not None and len(snippet) <= 20 and snippet.endswith("…") for snippet in snippets.values())


async def test_another_knowledge_bases_row_leaves_this_one_on_the_default(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _knowledge_base_tuning(admin, _OTHER_COLLECTION, {"max_hits_per_document": 0, "folded_snippet_chars": 20})

    rows = await _search(reader)

    assert sorted(_documents(rows)) == ["alpha", "beta", "delta", "epsilon", "gamma", "other"]
    assert _folded_snippets(_alpha(rows)) == {unit_id: _TEXT_OF[unit_id] for unit_id in _folded_snippets(_alpha(rows))}


async def test_a_search_over_every_knowledge_base_caps_each_document_by_its_own_knowledge_base(
    admin: asyncpg.Connection, reader: asyncpg.Connection
):
    await admin.execute(
        "INSERT INTO kb.sources_raw (collection, source_id, title, source_hash, provider, provider_key, meta)"
        " VALUES ($1, 'src.log', 'src.log', 'h', 'intake', 'src.log', '{}'::jsonb)",
        _OTHER_COLLECTION,
    )
    await admin.execute(
        "INSERT INTO kb.raw_chunks (collection, id, source_id, seq, section_path, heading, body)"
        " VALUES ($1, 'src.log:0', 'src.log', 0, 'Body'::ltree, 'Body', $2)",
        _OTHER_COLLECTION,
        f"{_WORD} in the chunk of src.log",
    )
    await admin.execute(
        "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, source_id)"
        " VALUES ($1, 'note.log', 'note', 'note.log', 'note.log', 'live', $2, 'h', 'src.log')",
        _OTHER_COLLECTION,
        f"{_WORD} on the card of note.log",
    )
    await _knowledge_base_tuning(admin, _OTHER_COLLECTION, {"max_hits_per_document": 0})

    rows = await reader.fetch("SELECT * FROM api.search($1, k => 50, collection => NULL)", _WORD)

    assert sorted(str(row["id"]) for row in rows if row["src_collection"] == _OTHER_COLLECTION) == ["note.log", "src.log:0"]
    assert sorted(_documents([row for row in rows if row["src_collection"] == _COLLECTION])) == ["alpha", "beta", "delta", "epsilon", "gamma", "other"]


async def test_the_reader_reads_one_knowledge_bases_cap_its_sessions_own_by_default(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _knowledge_base_tuning(admin, _COLLECTION, {"max_hits_per_document": 2})

    named = await reader.fetchval("SELECT api.search_hits_per_document($1)", _COLLECTION)
    other = await reader.fetchval("SELECT api.search_hits_per_document($1)", _OTHER_COLLECTION)
    unnamed = await reader.fetchval("SELECT api.search_hits_per_document(NULL)")
    await reader.execute("SELECT set_config('app.collection', $1, false)", _COLLECTION)
    session = await reader.fetchval("SELECT api.search_hits_per_document()")

    assert (named, other, unnamed, session) == (2, 1, 1, 2)


async def test_the_tuning_table_keys_one_default_row_and_one_row_per_knowledge_base(admin: asyncpg.Connection):
    rows = await admin.fetch("SELECT collection, tuning FROM kb.retrieval_tuning")
    await _knowledge_base_tuning(admin, _COLLECTION, {"max_hits_per_document": 2, "folded_snippet_chars": 120})
    await _knowledge_base_tuning(admin, _COLLECTION, {"max_hits_per_document": 3})

    with pytest.raises(asyncpg.UniqueViolationError):
        await admin.execute("INSERT INTO kb.retrieval_tuning (collection, tuning) VALUES (NULL, '{}'::jsonb)")
    with pytest.raises(asyncpg.CheckViolationError):
        await _knowledge_base_tuning(admin, _OTHER_COLLECTION, {"budget": 20})
    with pytest.raises(asyncpg.CheckViolationError):
        await _knowledge_base_tuning(admin, _OTHER_COLLECTION, {"folded_snippet_chars": "long"})

    (default,) = rows
    own = json.loads(str(await admin.fetchval("SELECT tuning FROM kb.retrieval_tuning WHERE collection = $1", _COLLECTION)))
    assert default["collection"] is None
    assert json.loads(str(default["tuning"]))["folded_snippet_chars"] == 160
    assert own == {"max_hits_per_document": 3, "folded_snippet_chars": 120}


async def test_0071_makes_the_single_row_the_default_row(migrated: str, admin: asyncpg.Connection):
    run_downgrade(migrated, "0070")
    await admin.execute("""UPDATE kb.retrieval_tuning SET tuning = tuning || '{"budget": 77}'::jsonb WHERE id = 1""")
    run_upgrade_head(migrated)

    (row,) = await admin.fetch("SELECT collection, tuning FROM kb.retrieval_tuning")

    tuning = json.loads(str(row["tuning"]))
    assert row["collection"] is None
    assert (tuning["budget"], tuning["max_hits_per_document"], tuning["folded_snippet_chars"]) == (77, 1, 160)


async def test_the_comments_name_the_folded_snippet_and_the_cap_of_each_knowledge_base(migrated: str, admin: asyncpg.Connection):
    comment = await _comment(migrated)
    table = str(await admin.fetchval(_TABLE_COMMENT))

    assert "{id, hit_level, snippet}" in comment
    assert "the owner sets for each knowledge base or for the whole database" in comment
    assert "folded_snippet_chars" in table
    assert "may set only max_hits_per_document and folded_snippet_chars" in table


async def test_one_step_down_puts_0070s_search_single_row_cap_function_and_comments_back(
    migrated: str, admin: asyncpg.Connection, reader: asyncpg.Connection
):
    await _knowledge_base_tuning(admin, _COLLECTION, {"max_hits_per_document": 2})
    run_downgrade(migrated, "0070")
    alpha = _alpha(await _search(reader))
    rows = [(row["id"], json.loads(str(row["tuning"]))) for row in await admin.fetch("SELECT id, tuning FROM kb.retrieval_tuning")]
    cap = await reader.fetchval("SELECT api.search_hits_per_document()")
    taking_a_collection = await admin.fetchval("SELECT to_regprocedure('api.search_hits_per_document(text)')")
    comment = await _comment(migrated)
    table = str(await admin.fetchval(_TABLE_COMMENT))
    run_upgrade_head(migrated)

    previous = _load_migration_module("0070")
    assert all(snippet is None for snippet in _folded_snippets(alpha).values())
    assert len(_folded(alpha)) == 3
    assert [(row_id, "folded_snippet_chars" in tuning) for row_id, tuning in rows] == [(1, False)]
    assert cap == 1
    assert taking_a_collection is None
    assert comment == previous._COMMENT_TEXT
    assert f"COMMENT ON TABLE kb.retrieval_tuning IS '{table}'" == previous._TUNING_COMMENT
