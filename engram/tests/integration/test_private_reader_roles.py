"""Each private collection is read through its own login role, which the indexer provisions and nothing else grants.

Two private collections and one shared collection are seeded into one head-migrated database.

What this pins:

* provisioning creates a login role that belongs to `kb_reader`, carries the reader's own settings, passes the reader
  check, and gets one scope row;
* a private collection's role reads its own collection and the shared one, never the other private one;
* the owner reads every collection through the api surface, since it can read the tables anyway;
* provisioning twice changes nothing, and a collection that turns shared loses its scope row;
* one step down puts the setting-based predicate and its comment back, and one step up restores the role-based ones.
"""

from __future__ import annotations

import asyncpg

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.infrastructure.gateway import verify_reader_role
from engram.retrieval.infrastructure.reader_roles import SqlReaderScopes
from engram.retrieval.reader_roles import private_reader_role
from engram.testing.infrastructure.knowledge_base_db import TEST_READER_SECRET, provision_private_reader, run_downgrade, run_upgrade_head
from tests.integration.test_migrations import _asyncpg_dsn

_FIRST = "first-private"
_SECOND = "second-private"
_SHARED = "shared"

_SWITCHES = '{"embeddings": false, "sql": false, "graph": false, "private": %s}'


async def _seed(admin_url: str) -> None:
    """One page in each of the three collections, and the fingerprints that mark two of them private."""
    connection = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        await connection.executemany(
            "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets)"
            " VALUES ($1, $2, 'entity', $2, $2, 'live', $2, $2, '{}'::jsonb)",
            [(collection, f"ent.{collection}") for collection in (_FIRST, _SECOND, _SHARED)],
        )
        await connection.executemany(
            "INSERT INTO kb.index_fingerprint (collection, indexer_version, spec_hash, vocabulary_hash, embed_model, switches)"
            " VALUES ($1, '1', 's', 'r', 'm', $2::jsonb)",
            [(_FIRST, _SWITCHES % "true"), (_SECOND, _SWITCHES % "true"), (_SHARED, _SWITCHES % "false")],
        )
    finally:
        await connection.close()


async def _collections(url: str) -> list[str]:
    """The collections whose cards a connection on `url` reads, in order."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch("SELECT src_collection FROM api.cards ORDER BY src_collection")
    finally:
        await connection.close()
    return [row["src_collection"] for row in rows]


async def _fetch(url: str, sql: str, *args: object) -> list[asyncpg.Record]:
    """The rows `sql` answers on a fresh connection to `url`."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return await connection.fetch(sql, *args)
    finally:
        await connection.close()


async def test_provisioning_creates_a_confined_login_role_with_one_scope(migrated: str):
    await _seed(migrated)
    url = await provision_private_reader(migrated, _FIRST)
    role = private_reader_role(_FIRST)

    [granted] = await _fetch(
        migrated,
        "SELECT r.rolcanlogin, r.rolconfig, pg_has_role(r.oid, 'kb_reader', 'MEMBER') AS member,"
        " (SELECT rolconfig FROM pg_roles WHERE rolname = 'kb_reader') AS reader_config"
        " FROM pg_roles r WHERE r.rolname = $1",
        role,
    )
    scopes = await _fetch(migrated, "SELECT role_name, collection FROM kb.reader_scopes")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await verify_reader_role(engine)
    finally:
        await engine.dispose()

    assert granted["rolcanlogin"]
    assert granted["member"]
    assert sorted(granted["rolconfig"]) == sorted(granted["reader_config"])
    assert [(row["role_name"], row["collection"]) for row in scopes] == [(role, _FIRST)]


async def test_a_private_role_reads_its_own_collection_and_the_shared_one_only(migrated: str):
    await _seed(migrated)
    first = await provision_private_reader(migrated, _FIRST)
    second = await provision_private_reader(migrated, _SECOND)

    assert await _collections(first) == [_FIRST, _SHARED]
    assert await _collections(second) == [_SECOND, _SHARED]


async def test_the_anonymous_reader_reads_only_the_shared_collection(migrated: str, reader_url: str):
    await _seed(migrated)
    await provision_private_reader(migrated, _FIRST)

    assert await _collections(reader_url) == [_SHARED]


async def test_the_owner_reads_every_collection(migrated: str):
    await _seed(migrated)

    assert await _collections(migrated) == [_FIRST, _SECOND, _SHARED]


async def test_provisioning_twice_changes_nothing_and_a_shared_collection_loses_its_scope(migrated: str):
    await _seed(migrated)
    url = await provision_private_reader(migrated, _FIRST)
    await provision_private_reader(migrated, _FIRST)
    twice = await _fetch(migrated, "SELECT role_name FROM kb.reader_scopes")

    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        await SqlReaderScopes(engine, TEST_READER_SECRET).scope(_FIRST, private=False)
    finally:
        await engine.dispose()
    after = await _fetch(migrated, "SELECT role_name FROM kb.reader_scopes")

    assert len(twice) == 1
    assert after == []
    assert await _collections(url) == [_SHARED]


async def test_one_step_down_and_back_swaps_the_predicate_and_its_comment(migrated: str):
    comment = "SELECT obj_description('api.visible_collection(text)'::regprocedure, 'pg_proc') AS comment"
    [at_head] = await _fetch(migrated, comment)

    run_downgrade(migrated, "0054")
    [below] = await _fetch(migrated, comment)
    [table] = await _fetch(migrated, "SELECT to_regclass('kb.reader_scopes') AS name")
    run_upgrade_head(migrated)
    [again] = await _fetch(migrated, comment)

    assert "own reader role" in at_head["comment"]
    assert "app.collection" in below["comment"]
    assert table["name"] is None
    assert again["comment"] == at_head["comment"]
