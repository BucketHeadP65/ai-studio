"""Integration test for the 0006 index-state migration's up/down round-trip, plus 0032's addition.

Drives ``alembic upgrade head`` against a throwaway local Postgres container
(the ``migrated`` fixture from ``tests/conftest.py``), then introspects
``information_schema`` / ``pg_indexes`` to prove migration ``0006`` created the
four index-private tables (``index_manifest``, ``link_refs``, ``validation_store``,
``index_fingerprint``) and their named indexes in schema ``memory``.

``TestRetrievalSwitchesColumn`` covers ``0032``, layered on top: ``index_fingerprint`` gains a
``switches`` column, and ``api.retrieval_switches`` is the one reader-callable door onto it — the
index-state tables themselves stay ungranted (0006), this function is the sole exception.

``TestPageTypeContractsMigration`` covers ``0036``: ``kb.page_type_contracts`` (ungranted, like
its index-state siblings) and ``api.page_types``, the one reader-granted view onto it.

The down/up half is the chain-validity oracle: ``alembic downgrade 0005`` must drop
all four tables (unapplying every revision above ``0005``, however many the chain has
grown), and a second ``alembic upgrade head`` must re-create them cleanly. Those two
extra alembic commands reuse the ``config.attributes`` seam ``tests/conftest.py``
threads (the target URL plus the bed's BM25 dialect + graph backend) and run on a
loop-free worker thread, because ``alembic/env.py``'s ``run_migrations_online`` calls
``asyncio.run()`` and ``asyncio_mode = auto`` already runs these tests inside a loop.

The whole module skips when Docker is unavailable (the ``pg`` fixture raises
``pytest.skip``).
"""

from __future__ import annotations

import json

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import pytest

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from engram.testing.infrastructure.knowledge_base_db import BED_GRAPH_BACKEND

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# The bed's BM25 dialect, mirroring `tests/conftest.py`'s `_BED_DIALECT`: the analog migrates under
# `pg_textsearch`, the opt-in ParadeDB bed under `pg_search`. Derived from the public `BED_GRAPH_BACKEND`
# (`age` ⇒ analog/`pg_textsearch`, `cte` ⇒ ParadeDB/`pg_search`) so a bed switch needs no edit here.
_BED_DIALECT = "pg_textsearch" if BED_GRAPH_BACKEND == "age" else "pg_search"

# `engram/` — three levels up from `tests/integration/index/`; the alembic.ini lives here.
_ENGRAM_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _ENGRAM_ROOT / "alembic.ini"
_ALEMBIC_DIR = _ENGRAM_ROOT / "alembic"

# The four index-private tables 0006 adds to the `kb` schema.
EXPECTED_COMPILE_STATE_TABLES = frozenset({"index_manifest", "link_refs", "validation_store", "index_fingerprint"})

# The read-only serving role, granted EXECUTE on api.retrieval_switches despite index_fingerprint
# itself carrying no reader grant (mirrors 0006's "no role grants" posture on the index-state tables).
_READER_ROLE = "kb_reader"

# Every named index-state index a database at head carries: the ones 0006 creates (one per FK
# plus the closure-query indexes), with the manifest's source-key index under the name 0044 gives it.
EXPECTED_COMPILE_STATE_INDEXES = frozenset(
    {
        "ix_index_manifest_page",
        "ix_index_manifest_source_key",
        "ix_link_refs_target",
        "ix_link_refs_src",
        "ix_link_refs_dst",
        "ix_validation_store_page",
        "ix_validation_store_severity",
    }
)


def _alembic_config(url: str) -> Config:
    """Build an alembic Config pinned to the app's ini + the throwaway DB and the bed's grammar.

    Mirrors ``tests/conftest.py::_run_upgrade_head_sync``: threads the target URL and the bed's
    ``dialect`` / ``graph_backend`` through ``config.attributes`` (the seam ``alembic/env.py``
    honours), so the down/up half migrates under the same grammar as the initial ``upgrade head``.
    """
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    config.attributes["sqlalchemy_url"] = url
    config.attributes["dialect"] = _BED_DIALECT
    config.attributes["graph_backend"] = BED_GRAPH_BACKEND
    return config


def _run_alembic(url: str, revision: str, *, downgrade: bool) -> None:
    """Run ``alembic upgrade``/``downgrade`` to ``revision`` against ``url`` on a loop-free thread.

    ``alembic/env.py``'s ``run_migrations_online`` calls ``asyncio.run()``; under
    ``asyncio_mode = auto`` this test already runs inside a pytest-asyncio loop, so the command
    is offloaded to a fresh worker thread (which has no running loop, making the nested
    ``asyncio.run()`` legal) and blocked on — the same guard ``tests/conftest.py`` documents.
    """
    config = _alembic_config(url)
    run = command.downgrade if downgrade else command.upgrade
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run, config, revision).result()


def _chain_head(url: str) -> str:
    """The migration chain's head revision, read from the scripts so a new revision never stales this module."""
    head = ScriptDirectory.from_config(_alembic_config(url)).get_current_head()
    assert head is not None, "alembic script directory has no head revision"
    return head


@pytest.fixture
async def conn(migrated: str) -> AsyncIterator[asyncpg.Connection]:
    """An asyncpg connection to the migrated database (admin URL)."""
    connection = await asyncpg.connect(migrated.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        yield connection
    finally:
        await connection.close()


async def _kb_tables(conn: asyncpg.Connection) -> set[str]:
    """The set of table names in the ``kb`` schema."""
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'kb'")
    return {row[0] for row in rows}


async def _kb_indexes(conn: asyncpg.Connection) -> set[str]:
    """The set of index names in the ``kb`` schema."""
    rows = await conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = 'kb'")
    return {row[0] for row in rows}


class TestCompileStateMigration:
    """0006 creates the four index-private tables + indexes and round-trips cleanly."""

    async def test_compile_state_tables_present_after_upgrade(self, conn: asyncpg.Connection) -> None:
        """`upgrade head` (the `migrated` fixture) creates all four index-state tables in `kb`."""
        tables = await _kb_tables(conn)

        assert tables >= EXPECTED_COMPILE_STATE_TABLES, f"missing index-state tables: {EXPECTED_COMPILE_STATE_TABLES - tables}"

    async def test_compile_state_indexes_present_after_upgrade(self, conn: asyncpg.Connection) -> None:
        """Every named index-state index exists in `memory`, under the name a database at head carries."""
        indexes = await _kb_indexes(conn)

        assert indexes >= EXPECTED_COMPILE_STATE_INDEXES, f"missing index-state indexes: {EXPECTED_COMPILE_STATE_INDEXES - indexes}"

    async def test_link_refs_src_fk_cascades(self, conn: asyncpg.Connection) -> None:
        """`link_refs.src_page_id` carries the `ON DELETE CASCADE` FK to `kb.pages`."""
        rule = await conn.fetchval(
            "SELECT rc.delete_rule"
            " FROM information_schema.referential_constraints rc"
            " JOIN information_schema.table_constraints tc"
            "   ON tc.constraint_name = rc.constraint_name AND tc.constraint_schema = rc.constraint_schema"
            " WHERE tc.table_schema = 'kb' AND tc.table_name = 'link_refs' AND tc.constraint_type = 'FOREIGN KEY'"
        )

        assert rule == "CASCADE"

    async def test_downgrade_drops_all_four_tables(self, migrated: str) -> None:
        """`alembic downgrade 0005` removes every index-state table (unapplying 0006 and everything above it)."""
        _run_alembic(migrated, "0005", downgrade=True)

        connection = await asyncpg.connect(migrated.replace("postgresql+asyncpg://", "postgresql://", 1))
        try:
            tables = await _kb_tables(connection)
            version = await connection.fetchval("SELECT version_num FROM alembic_version")
        finally:
            await connection.close()

        assert tables.isdisjoint(EXPECTED_COMPILE_STATE_TABLES), (
            f"downgrade left index-state tables: {tables & EXPECTED_COMPILE_STATE_TABLES}"
        )
        assert version == "0005"

    async def test_upgrade_downgrade_upgrade_round_trips_clean(self, migrated: str) -> None:
        """down→up replays cleanly: after `downgrade 0005` then `upgrade head` the four tables are back at head."""
        _run_alembic(migrated, "0005", downgrade=True)
        _run_alembic(migrated, "head", downgrade=False)

        connection = await asyncpg.connect(migrated.replace("postgresql+asyncpg://", "postgresql://", 1))
        try:
            tables = await _kb_tables(connection)
            indexes = await _kb_indexes(connection)
            version = await connection.fetchval("SELECT version_num FROM alembic_version")
        finally:
            await connection.close()

        assert tables >= EXPECTED_COMPILE_STATE_TABLES
        assert indexes >= EXPECTED_COMPILE_STATE_INDEXES
        assert version == _chain_head(migrated)


class TestRetrievalSwitchesColumn:
    """0032: `index_fingerprint.switches` and its one reader door, `api.retrieval_switches`."""

    async def test_switches_column_exists_as_not_null_jsonb_defaulted_empty(self, conn: asyncpg.Connection) -> None:
        row = await conn.fetchrow(
            "SELECT data_type, is_nullable, column_default"
            " FROM information_schema.columns"
            " WHERE table_schema = 'kb' AND table_name = 'index_fingerprint' AND column_name = 'switches'"
        )

        assert row is not None, "kb.index_fingerprint has no switches column"
        assert row["data_type"] == "jsonb"
        assert row["is_nullable"] == "NO"
        assert row["column_default"] == "'{}'::jsonb"

    async def test_retrieval_switches_function_exists_in_api_schema(self, conn: asyncpg.Connection) -> None:
        exists = await conn.fetchval(
            "SELECT count(*) > 0 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'api' AND p.proname = 'retrieval_switches'"
        )

        assert exists is True

    async def test_kb_reader_may_execute_retrieval_switches(self, conn: asyncpg.Connection) -> None:
        can = await conn.fetchval("SELECT has_function_privilege($1, 'api.retrieval_switches(text)', 'EXECUTE')", _READER_ROLE)

        assert can is True, f"{_READER_ROLE} was not granted EXECUTE on api.retrieval_switches"

    async def test_returns_empty_object_when_the_collection_has_no_fingerprint_row(self, conn: asyncpg.Connection) -> None:
        result = await conn.fetchval("SELECT api.retrieval_switches('no-such-collection')::text")

        assert result is not None
        assert json.loads(result) == {}

    async def test_returns_the_row_s_switches_when_present(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            "INSERT INTO kb.index_fingerprint (collection, indexer_version, spec_hash, vocabulary_hash, embed_model, switches)"
            " VALUES ('research', '1.0.0', 'spec-1', 'reg-1', 'embed-1', '{\"embeddings\": true, \"sql\": false, \"graph\": true}'::jsonb)"
        )

        result = await conn.fetchval("SELECT api.retrieval_switches('research')::text")

        assert result is not None
        assert json.loads(result) == {"embeddings": True, "sql": False, "graph": True}


_FLAG_CONTRACT = json.dumps(
    {
        "page_type": "flag",
        "id_prefix": "flag.",
        "folder": "Wiki/Flags",
        "folders": [],
        "files": [],
        "frontmatter_fields": [{"name": "flag_type", "type": "scalar", "required": True, "choices": ["idea", "feature"], "default": None}],
        "status_values": ["pending", "accepted", "dismissed"],
        "path_fields": [],
        "canonical_sections": ["why", {"slug": "proposal", "heading": "Proposal"}],
        "edge_fields": ["about"],
        "claims_enabled": False,
        "disclosed": True,
        "guidance": "",
    }
)


class TestPageTypeContractsMigration:
    """0036: `kb.page_type_contracts` (ungranted) and `api.page_types`, its one reader door."""

    async def test_page_type_contracts_table_exists_ungranted(self, conn: asyncpg.Connection) -> None:
        row = await conn.fetchrow(
            "SELECT data_type, is_nullable FROM information_schema.columns"
            " WHERE table_schema = 'kb' AND table_name = 'page_type_contracts' AND column_name = 'contract'"
        )
        assert row is not None, "kb.page_type_contracts has no contract column"
        assert row["data_type"] == "jsonb"
        assert row["is_nullable"] == "NO"

        can = await conn.fetchval("SELECT has_table_privilege($1, 'kb.page_type_contracts', 'SELECT')", _READER_ROLE)
        assert can is False, f"{_READER_ROLE} must not read kb.page_type_contracts directly"

    async def test_page_type_contracts_primary_key_is_collection_page_type(self, conn: asyncpg.Connection) -> None:
        rows = await conn.fetch(
            "SELECT kcu.column_name FROM information_schema.table_constraints tc"
            " JOIN information_schema.key_column_usage kcu"
            "   ON kcu.constraint_name = tc.constraint_name AND kcu.constraint_schema = tc.constraint_schema"
            " WHERE tc.table_schema = 'kb' AND tc.table_name = 'page_type_contracts' AND tc.constraint_type = 'PRIMARY KEY'"
        )
        assert {row["column_name"] for row in rows} == {"collection", "page_type"}

    async def test_page_types_view_exists_in_api_schema(self, conn: asyncpg.Connection) -> None:
        exists = await conn.fetchval("SELECT count(*) > 0 FROM pg_views WHERE schemaname = 'api' AND viewname = 'page_types'")
        assert exists is True

    async def test_kb_reader_may_select_page_types(self, conn: asyncpg.Connection) -> None:
        can = await conn.fetchval("SELECT has_table_privilege($1, 'api.page_types', 'SELECT')", _READER_ROLE)
        assert can is True, f"{_READER_ROLE} was not granted SELECT on api.page_types"

    async def test_view_extracts_typed_columns_from_a_written_contract(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            "INSERT INTO kb.page_type_contracts (collection, page_type, contract) VALUES ('research', 'flag', $1::jsonb)",
            _FLAG_CONTRACT,
        )

        row = await conn.fetchrow("SELECT * FROM api.page_types WHERE collection = 'research' AND page_type = 'flag'")

        assert row is not None
        assert row["id_prefix"] == "flag."
        assert row["folder"] == "Wiki/Flags"
        assert list(row["status_values"]) == ["pending", "accepted", "dismissed"]
        assert list(row["canonical_sections"]) == ["why", "proposal"]
        assert list(row["edge_fields"]) == ["about"]
        assert row["claims_enabled"] is False
        assert row["disclosed"] is True
        assert json.loads(row["frontmatter_fields"])[0]["name"] == "flag_type"
        assert json.loads(row["contract"])["page_type"] == "flag"

    async def test_absent_keys_come_back_empty_never_error(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            "INSERT INTO kb.page_type_contracts (collection, page_type, contract) VALUES ('research', 'bare', '{}'::jsonb)"
        )

        row = await conn.fetchrow("SELECT * FROM api.page_types WHERE collection = 'research' AND page_type = 'bare'")

        assert row is not None
        assert row["id_prefix"] == ""
        assert row["folder"] is None
        assert list(row["status_values"]) == []
        assert list(row["canonical_sections"]) == []
        assert list(row["edge_fields"]) == []
        assert row["claims_enabled"] is False
        assert row["disclosed"] is True
        assert json.loads(row["frontmatter_fields"]) == []

    async def test_downgrade_drops_view_then_table(self, migrated: str) -> None:
        _run_alembic(migrated, "0035", downgrade=True)

        connection = await asyncpg.connect(migrated.replace("postgresql+asyncpg://", "postgresql://", 1))
        try:
            view_gone = await connection.fetchval("SELECT count(*) = 0 FROM pg_views WHERE schemaname = 'api' AND viewname = 'page_types'")
            table_gone = await connection.fetchval("SELECT count(*) = 0 FROM pg_tables WHERE schemaname = 'kb' AND tablename = 'page_type_contracts'")
        finally:
            await connection.close()

        assert view_gone is True
        assert table_gone is True

        _run_alembic(migrated, "head", downgrade=False)
