"""Integration tests for the alembic migration chain on a fresh ParadeDB.

These tests run `alembic upgrade head` against a throwaway
`paradedb/paradedb:latest-pg17` container (the `migrated` fixture) and then
introspect `pg_catalog` / `information_schema` to prove the three migrations
transcribed the contract DDL faithfully:

* `0001` — the `kb` schema, the `pg_search`/`vector`/`ltree`
  extensions, the 10 base tables, and the BM25 / HNSW / GIN / GIST indexes;
* `0002` — the `api` schema, its views + functions, and the COMMENTs that
  *are* the M-Schema;
* `0003` + `0014` — the `kb_reader` role with `default_transaction_read_only`.

The whole module skips when Docker is unavailable (the `pg` fixture raises
`pytest.skip`), so the unit suite stays green on a Docker-less machine.
`asyncio_mode = auto` makes the `async def` tests run without a decorator;
each opens its own asyncpg connection to the migrated database.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import uuid

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import pytest
import sqlalchemy.exc

from alembic.config import Config
from alembic.script import ScriptDirectory
from core.ai.retrieval.contracts import (
    AgeGraphTraversalBuilder,
    CteGraphTraversalBuilder,
    ParadeDBHybridSearchBuilder,
    hits_per_document_body,
)
from core.ai.retrieval.search_builder import SEARCH_SNIPPET_CHARS

from engram.retrieval.graph_schema import kb_graph_schema
from engram.retrieval.knowledge_base_schema import KB_SEARCH_SCHEMA
from engram.testing.infrastructure.knowledge_base_db import (
    BED_BM25_EXTENSION,
    BED_GRAPH_BACKEND,
    new_test_database,
    reader_url_for,
    run_downgrade,
    run_upgrade,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from types import ModuleType

    from core.ai.retrieval.contracts import GraphSchema, SearchSchema
    from pydantic import JsonValue

# `engram/alembic/versions` — three levels up from `tests/integration/`, alongside the migration files
# the drift-guard test loads directly (see `_load_migration_module`).
_ALEMBIC_VERSIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

# The gated `pg_textsearch` profile only runs when this points at a reachable `pg_textsearch` database's admin URL;
# absent (the normal case), the `pg_textsearch` parametrization skips.
_PG_TEXTSEARCH_URL_ENV = "WIKI_PG_TEXTSEARCH_URL"

# The local Postgres admin URL. When set, the gated `age` graph profile RUNS against it
# (an AGE + pg_textsearch image, docker-compose `postgres` on host port 5434) instead of
# skipping — ParadeDB, the default test container, ships no Apache AGE.
_ANALOG_URL_ENV = "WIKI_LOCAL_POSTGRES_URL"

# The 10 base tables of the `kb` schema, per the contract's "Base tables" section.
EXPECTED_MEMORY_TABLES = frozenset(
    {"sources_raw", "raw_chunks", "pages", "sections", "claims", "edges", "vocab", "embed_meta", "query_log"}
)

# The claim-qualifier table 0024 adds to the `kb` schema (one row per resolved qualifier target).
EXPECTED_SPINE_MATURATION_TABLES = frozenset({"claim_qualifiers"})

# The two evaluation-run summary tables 0005 adds to the `kb` schema.
EXPECTED_EVAL_TABLES = frozenset({"evaluation_runs", "evaluation_case_results"})

# The index-private tables in the `kb` schema: the four 0006 adds (true incremental
# ingestion) plus the per-page_type contract store 0036 adds.
EXPECTED_COMPILE_STATE_TABLES = frozenset({"index_manifest", "link_refs", "validation_store", "index_fingerprint", "page_type_contracts"})

# The indexed, pending-governed disclosure-summary table 0011 adds to the `kb` schema.
EXPECTED_UNIT_SUMMARY_TABLES = frozenset({"unit_summaries"})

# The table 0055 adds: which private collection each reader role may read.
EXPECTED_READER_SCOPE_TABLES = frozenset({"reader_scopes"})

# The table 0059 adds: the numbers api.search ranks with.
EXPECTED_TUNING_TABLES = frozenset({"retrieval_tuning"})

# The table 0069 adds: the cut plans a chunk planner chose.
EXPECTED_CUT_PLAN_TABLES = frozenset({"cut_plans"})

# Every `api` view + function the serving layer depends on (contract §Agent-facing surface).
EXPECTED_API_VIEWS = frozenset(
    {
        "about",
        "schema_guide",
        "cards",
        "sections",
        "claims",
        "edges",
        "contradictions",
        "raw_chunks",
        "stale",
    }
)

# `envelope_collection` / `envelope_page` / `envelope_section` are the three SECURITY DEFINER
# functions migration 0012 adds for the reader-role progressive-disclosure surface (`EnvelopeHandler`,
# `engram/retrieval/envelope.py`) — pinned here explicitly, not left to the `>=` superset check alone,
# the same allowlist-drift class that let the reader-permission gap (fixed by 0012 itself) ship
# unnoticed: a superset assertion with nothing NAMING these three would keep passing even if a future
# migration silently dropped or renamed one of them.
EXPECTED_API_FUNCTIONS = frozenset(
    {
        "search",
        "lookup_value",
        "fetch",
        "log_session",
        "envelope_collection",
        "envelope_page",
        "envelope_section",
        "collection_about",
        "collection_schema_guide",
        "collection_authoring_rules",
    }
)

# The link-graph traversal functions 0004_graph.py adds to `api` (backend-routed CTE/AGE).
EXPECTED_GRAPH_FUNCTIONS = frozenset({"graph_expand", "graph_paths"})

# The `api.graph_*` signatures a database at head carries, byte-identical to the GRANT/DROP
# signatures in 0048_focused_retrieval_results.py — what `has_function_privilege` resolves against.
# 0025 prepends `p_collection text`, scoping every traversal to one collection; 0048 appends
# `include_superseded boolean`, the argument a caller asks a superseded node with.
_EXPAND_SIGNATURE = "api.graph_expand(text, text[], integer, text[], integer, boolean)"
_PATHS_SIGNATURE = "api.graph_paths(text, text, text, integer, text[], integer, boolean)"

# The five-argument signatures 0048 replaces. A database at head must carry neither: two overloads
# differing only by a defaulted trailing argument make every call that omits it ambiguous.
_PREVIOUS_EXPAND_SIGNATURE = "api.graph_expand(text, text[], integer, text[], integer)"
_PREVIOUS_PATHS_SIGNATURE = "api.graph_paths(text, text, text, integer, text[], integer)"

# The generated walk each `api` wrapper calls, ungranted and reached only through the wrapper.
_EXPAND_WALK_SIGNATURE = "kb.graph_expand_walk(text, text[], integer, text[], integer, boolean)"
_PREVIOUS_EXPAND_WALK_SIGNATURE = "kb.graph_expand_walk(text, text[], integer, text[], integer)"

# The four columns 0048 adds to `api.search`'s result: each leg's own rank behind the fused score,
# the semantic leg's raw distance, and whether the bounded snippet was cut.
_SEARCH_RESULT_FACTS = ("lexical_rank", "vector_rank", "vector_distance", "snippet_truncated")

# The Apache AGE property graph 0004 projects under the `age` backend (`create_graph('kb_graph')`).
_AGE_GRAPH_NAME = "kb_graph"

# The env seam alembic/env.py reads to pick the graph backend (APP_RETRIEVAL__GRAPH_BACKEND override / retrieval.graph_backend).
_GRAPH_BACKEND_ENV = "APP_RETRIEVAL__GRAPH_BACKEND"

# The read-only login the chain leaves behind; the graph functions are SECURITY DEFINER and 0004 grants it EXECUTE.
_READER_ROLE = "kb_reader"


@pytest.fixture
async def conn(migrated: str) -> AsyncIterator[asyncpg.Connection]:
    """An asyncpg connection to the migrated database (admin URL)."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        yield connection
    finally:
        await connection.close()


def _asyncpg_dsn(url: str) -> str:
    """Convert a SQLAlchemy `+asyncpg` URL into a DSN raw `asyncpg.connect` accepts.

    Strips the `+asyncpg` driver tag and rewrites SQLAlchemy's `ssl=<mode>` query
    param (the asyncpg-dialect spelling) to libpq's `sslmode=<mode>`: raw asyncpg
    treats a bare `ssl` query param as a *server* runtime parameter and raises
    `CantChangeRuntimeParamError` on connect. A managed database usually requires TLS, so the gated
    profile's URL may carry `ssl=require`; the local ParadeDB URL has no ssl param and
    so is returned unchanged.
    """
    plain = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parts = urlsplit(plain)
    if not parts.query:
        return plain
    query = [("sslmode", value) if key == "ssl" else (key, value) for key, value in parse_qsl(parts.query)]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _names(conn: asyncpg.Connection, sql: str, *args: object) -> set[str]:
    """Run a single-column query and collect the values into a set."""
    rows = await conn.fetch(sql, *args)
    return {row[0] for row in rows}


class TestSchemasAndExtensions:
    async def test_memory_and_api_schemas_exist(self, conn: asyncpg.Connection):
        schemas = await _names(conn, "SELECT nspname FROM pg_namespace")

        assert {"kb", "api"} <= schemas

    async def test_required_extensions_installed(self, conn: asyncpg.Connection):
        # The BM25 extension is dialect-routed (`pg_search` on ParadeDB, `pg_textsearch` on the
        # analog), so it is asserted by the bed's `BED_BM25_EXTENSION` rather than hard-coded;
        # `vector` / `ltree` are backend-agnostic and pinned to `public` by 0001.
        extensions = await _names(conn, "SELECT extname FROM pg_extension")

        assert {BED_BM25_EXTENSION, "vector", "ltree"} <= extensions


class TestBaseTables:
    async def test_all_memory_tables_present(self, conn: asyncpg.Connection):
        tables = await _names(conn, "SELECT tablename FROM pg_tables WHERE schemaname = 'kb'")

        assert (
            tables
            == EXPECTED_MEMORY_TABLES | EXPECTED_EVAL_TABLES | EXPECTED_COMPILE_STATE_TABLES | EXPECTED_UNIT_SUMMARY_TABLES
            | EXPECTED_SPINE_MATURATION_TABLES | EXPECTED_READER_SCOPE_TABLES | EXPECTED_TUNING_TABLES | EXPECTED_CUT_PLAN_TABLES
        )

    async def test_pages_carries_halfvec_embedding(self, conn: asyncpg.Connection):
        # The typed embedding column proves pgvector's `halfvec` type resolved in DDL.
        udt = await conn.fetchval(
            "SELECT udt_name FROM information_schema.columns"
            " WHERE table_schema = 'kb' AND table_name = 'pages' AND column_name = 'embedding'"
        )

        assert udt == "halfvec"

    async def test_sections_path_is_ltree(self, conn: asyncpg.Connection):
        udt = await conn.fetchval(
            "SELECT udt_name FROM information_schema.columns"
            " WHERE table_schema = 'kb' AND table_name = 'sections' AND column_name = 'path'"
        )

        assert udt == "ltree"

    async def test_sections_page_fk_cascades(self, conn: asyncpg.Connection):
        # ON DELETE CASCADE on sections.page_id → pages is part of the contract DDL.
        rule = await conn.fetchval(
            "SELECT rc.delete_rule"
            " FROM information_schema.referential_constraints rc"
            " JOIN information_schema.table_constraints tc"
            "   ON tc.constraint_name = rc.constraint_name AND tc.constraint_schema = rc.constraint_schema"
            " WHERE tc.table_schema = 'kb' AND tc.table_name = 'sections' AND tc.constraint_type = 'FOREIGN KEY'"
        )

        assert rule == "CASCADE"


class TestIndexes:
    async def _indexes(self, conn: asyncpg.Connection) -> dict[str, str]:
        """Map of `memory` index name → its `CREATE INDEX` definition."""
        rows = await conn.fetch("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'kb'")
        return {row["indexname"]: row["indexdef"] for row in rows}

    async def test_bm25_index_exists(self, conn: asyncpg.Connection):
        indexes = await self._indexes(conn)

        assert "pages_bm25" in indexes
        assert "USING bm25" in indexes["pages_bm25"]

    async def test_hnsw_index_exists(self, conn: asyncpg.Connection):
        indexes = await self._indexes(conn)

        assert "pages_hnsw" in indexes
        assert "USING hnsw" in indexes["pages_hnsw"]

    async def test_gist_ltree_index_exists(self, conn: asyncpg.Connection):
        indexes = await self._indexes(conn)

        assert "sections_path" in indexes
        assert "USING gist" in indexes["sections_path"]

    async def test_gin_facet_index_exists(self, conn: asyncpg.Connection):
        # 0024 folds the five facet arrays into one `pages.facets` jsonb map, GIN-indexed —
        # replacing the two per-array indexes (`pages_tasks` / `pages_stages`) 0001 created.
        indexes = await self._indexes(conn)

        assert "pages_facets" in indexes
        assert "USING gin" in indexes["pages_facets"]


class TestApiSurface:
    async def test_all_api_views_present(self, conn: asyncpg.Connection):
        views = await _names(conn, "SELECT table_name FROM information_schema.views WHERE table_schema = 'api'")

        assert views >= EXPECTED_API_VIEWS

    async def test_all_api_functions_present(self, conn: asyncpg.Connection):
        functions = await _names(
            conn, "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'api'"
        )

        assert functions >= EXPECTED_API_FUNCTIONS

    async def test_search_accepts_optional_query_vector(self, conn: asyncpg.Connection):
        # P9: api.search gains `query_vec halfvec DEFAULT NULL` — assert the arg type list carries halfvec.
        arg_types = await conn.fetchval(
            "SELECT pg_get_function_arguments(p.oid)"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'api' AND p.proname = 'search'"
        )

        assert arg_types is not None
        assert "halfvec" in arg_types

    async def test_search_narrows_by_one_jsonb_facets_parameter(self, conn: asyncpg.Connection):
        # 0024: the three research-slot params (task/stage/domain) collapse into one jsonb `facets`
        # bag — assert the declared arg list carries it (and none of the demoted slot names).
        arg_types = await conn.fetchval(
            "SELECT pg_get_function_arguments(p.oid)"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'api' AND p.proname = 'search'"
        )

        assert arg_types is not None
        assert "facets jsonb" in arg_types
        assert "task text" not in arg_types
        assert "stage text" not in arg_types
        assert "domain text" not in arg_types

    async def test_search_returns_the_per_leg_facts_and_the_truncation_flag(self, conn: asyncpg.Connection):
        # 0048: a hit says which legs selected it (a NULL rank means that leg did not), how far the
        # semantic leg placed it, and whether its snippet was cut — all beside the fused score.
        result_columns = await conn.fetchval(
            "SELECT pg_get_function_result(p.oid)"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'api' AND p.proname = 'search'"
        )

        assert result_columns is not None
        assert [column for column in _SEARCH_RESULT_FACTS if column not in result_columns] == []

    async def test_the_search_comment_names_the_columns_the_excerpt_bound_and_the_currency_default(self, conn: asyncpg.Connection):
        # `describe_schema` serves this comment to an agent, so a column the function returns and the
        # comment never names is a column no caller learns to read. The excerpt bound is read off the
        # builder, so a change to how much text a snippet carries fails here rather than drifting.
        comment = await conn.fetchval(
            "SELECT obj_description(p.oid, 'pg_proc')"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'api' AND p.proname = 'search'"
        )

        assert comment is not None
        assert [phrase for phrase in (*_SEARCH_RESULT_FACTS, "only_current", str(SEARCH_SNIPPET_CHARS)) if phrase not in comment] == []

    async def test_claim_qualifiers_table_exists_with_its_composite_key(self, conn: asyncpg.Connection):
        # 0024: the three lossy typed claim columns (on_dataset/on_task/metric) move to rows here.
        columns = await _names(
            conn,
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'kb' AND table_name = 'claim_qualifiers'",
        )

        assert columns == {"collection", "claim_id", "qualifier", "ord", "target_collection", "target_id"}

    async def test_collection_about_answers_only_its_own_collection(self, conn: asyncpg.Connection):
        await conn.execute(
            "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash)"
            " VALUES ('meta.about@research', 'research', 'meta', 'about', 'About', 'verified', 'Covers brines; excludes wines.', 'h')"
        )

        own = await conn.fetchval("SELECT body FROM api.collection_about('research')")
        other = await conn.fetch("SELECT body FROM api.collection_about('workdocs')")

        assert own == "Covers brines; excludes wines."
        assert other == []

    async def test_two_non_default_collections_keep_distinct_about_rows(self, conn: asyncpg.Connection):
        """`api.collection_about` must look up the indexer's scoped id, not the bare literal.

        `kb.pages.id` sits in the composite `(collection, id)` key, but every collection's
        orientation id is scoped as `meta.about@<collection>` (`orientation_page_id`) regardless --
        this proves `api.collection_about` looks up that same scoped id, so any two collections
        indexed into the same database never collide.
        """
        await conn.execute(
            "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash)"
            " VALUES ('meta.about@repo-docs', 'repo-docs', 'meta', 'about', 'About', 'verified', 'Repo docs about text.', 'h1')"
        )
        await conn.execute(
            "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash)"
            " VALUES ('meta.about@field-notes', 'field-notes', 'meta', 'about', 'About', 'verified', 'Field notes about text.', 'h2')"
        )

        repo_docs = await conn.fetchval("SELECT body FROM api.collection_about('repo-docs')")
        field_notes = await conn.fetchval("SELECT body FROM api.collection_about('field-notes')")

        assert repo_docs == "Repo docs about text."
        assert field_notes == "Field notes about text."

    async def test_kb_reader_may_execute_collection_about(self, conn: asyncpg.Connection):
        can = await conn.fetchval("SELECT has_function_privilege($1, 'api.collection_about(text)', 'EXECUTE')", _READER_ROLE)

        assert can is True, f"{_READER_ROLE} was not granted EXECUTE on api.collection_about"

    async def test_api_objects_are_commented(self, conn: asyncpg.Connection):
        # The COMMENTs are the M-Schema; spot-check that several api objects carry one.
        commented = await self._commented_api_relations(conn)

        assert len({"about", "cards", "claims"} & commented) >= 3

    async def _commented_api_relations(self, conn: asyncpg.Connection) -> set[str]:
        """Names of `api` relations (views/tables) that have a non-empty object COMMENT."""
        rows = await conn.fetch(
            "SELECT c.relname"
            " FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'api' AND obj_description(c.oid, 'pg_class') IS NOT NULL"
        )
        return {row[0] for row in rows}


def _load_migration_module(revision: str) -> ModuleType:
    """Import an alembic version file as a plain module, found by its revision-number filename prefix.

    Migration files aren't a normal importable package — alembic loads them itself via its own
    script directory — so this mirrors that with the stdlib directly: find the one
    `<revision>_*.py` under `alembic/versions`, and exec it as a fresh module. The file's own
    top-level imports (`alembic`, `core.ai.retrieval`, `engram.retrieval.search_dialect`) are
    ordinary installed-package imports, so they resolve the same way any other import in this
    test environment would.
    """
    matches = sorted(_ALEMBIC_VERSIONS_DIR.glob(f"{revision}_*.py"))
    assert len(matches) == 1, f"expected exactly one {revision}_*.py under {_ALEMBIC_VERSIONS_DIR}, found {matches}"
    spec = importlib.util.spec_from_file_location(f"_migration_{revision}", matches[0])
    assert spec is not None, f"no import spec for {matches[0]}"
    assert spec.loader is not None, f"import spec for {matches[0]} carries no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bare_type_signature(function_name: str, schema: SearchSchema) -> str:
    """The bare-type `name(type, type, …)` signature the real builder renders `schema` under.

    Parses `BaseHybridSearchBuilder._signature`'s actual output (the parameter declarations a
    `CREATE FUNCTION` body really carries: `query text, k int DEFAULT 12, query_vec halfvec(1024)
    DEFAULT NULL, …`) down to just its types, in declaration order — the same shape every
    migration's hand-written `COMMENT ON FUNCTION` / `GRANT` / `DROP FUNCTION` signature string
    names (`halfvec(1024)` collapses to `halfvec`, matching how those strings already write it).
    Uses `ParadeDBHybridSearchBuilder` only for its dialect-agnostic `_signature` (neither dialect
    subclass overrides it), so the same result holds whichever dialect a migration actually runs
    under.
    """
    types: list[str] = []
    # One declaration per rendered line: a facet default may itself carry a comma (the session
    # collection setting does), so the line break is the separator, never the comma.
    for declaration in ParadeDBHybridSearchBuilder()._signature(schema).split(",\n"):
        tokens = declaration.strip().split()
        types.append(tokens[1].split("(", 1)[0])
    return f"{function_name}({', '.join(types)})"


def _canonical_types(signature: str) -> str:
    """One spelling per SQL type in a signature string, so `int` and `integer` compare equal.

    Postgres resolves both spellings to the same type, and the migrations are split between them
    (0002/0010/0024 write `int`, 0037 onwards write `integer`), so a comparison against the builder's
    own rendering normalizes before it compares.
    """
    return signature.replace("integer", "int")


def _bare_graph_signature(function_name: str, parameters: str) -> str:
    """The bare-type `name(type, type, …)` signature for one rendered graph parameter list.

    The graph counterpart of `_bare_type_signature`: parses the parameter declarations a traversal's
    `CREATE FUNCTION` really carries (`p_collection text, seed_ids text[], hops int DEFAULT 2, …`)
    down to just their types, in declaration order — the shape a migration's hand-written
    `COMMENT ON FUNCTION` / `GRANT` / `DROP FUNCTION` signature string names.

    Args:
        function_name: The schema-qualified function name to render the signature under.
        parameters: The rendered parameter declarations, comma-separated.

    Returns:
        The bare-type signature string.
    """
    types = [declaration.strip().split()[1] for declaration in parameters.split(",\n") if declaration.strip()]
    return f"{function_name}({', '.join(types)})"


class TestSearchSignatureDriftGuard:
    """Every migration's hand-written `api.search` signature string must match its own frozen schema.

    0002, 0010, and 0024 all render `CREATE FUNCTION api.search(...)` dynamically, through the
    library's hybrid-search builder, from a `SearchSchema` each migration freezes locally — but
    each migration's `COMMENT ON FUNCTION` / `GRANT` / `DROP FUNCTION` signature string next to
    that call is hand-written text, not derived from the same render. A future change to
    `core.ai.retrieval.search_builder`'s `_signature` (a new fixed parameter, a renamed default,
    …) would silently desync one of these migrations' actual function from its own hardcoded
    signature string — the exact failure class that blocked migration-apply for the whole
    integration tier before 0024 landed (0002's `COMMENT` had drifted from what its body actually
    created, because the body rendered from the library's live, evolving descriptor rather than a
    frozen one). Giving 0002 and 0024 their own frozen schemas (mirroring 0010's long-standing
    pattern) closes that specific hole; this guard is what keeps it closed against the next
    builder change, on all three migrations at once, rather than trusting each future revision to
    remember the lesson by hand.

    Pure Python — a frozen schema plus the real builder, no `CREATE FUNCTION`, no database — so
    this class runs even when Docker is unavailable, unlike the rest of this module.
    """

    def test_0002_frozen_schema_matches_its_hardcoded_comment_signature(self) -> None:
        module = _load_migration_module("0002")
        comment = next(s for s in module._COMMENT_STATEMENTS if s.startswith("COMMENT ON FUNCTION api.search("))
        signature = re.search(r"COMMENT ON FUNCTION (api\.search\([^)]*\))", comment)
        assert signature is not None

        assert _bare_type_signature("api.search", module._search_schema()) == signature.group(1)

    def test_0010_frozen_schema_matches_its_hardcoded_signature_constant(self) -> None:
        module = _load_migration_module("0010")

        assert _bare_type_signature("api.search", module._search_schema(schema_name="memory")) == module._SEARCH_SIGNATURE

    def test_0024_frozen_schema_matches_its_hardcoded_signature_constant(self) -> None:
        module = _load_migration_module("0024")

        assert _bare_type_signature("api.search", module._new_search_schema()) == module._NEW_SEARCH_SIGNATURE

    def test_0048_frozen_schema_matches_its_hardcoded_signature_constant(self) -> None:
        module = _load_migration_module("0048")

        assert _canonical_types(_bare_type_signature("api.search", module._search_schema())) == _canonical_types(module._SEARCH_SIGNATURE)

    def test_0049_frozen_schemas_match_their_hardcoded_signature_constants(self) -> None:
        module = _load_migration_module("0049")

        published = _bare_type_signature("api.search", module._search_schema(dated=True))
        restored = _bare_type_signature("api.search", module._search_schema(dated=False))
        assert _canonical_types(published) == _canonical_types(module._SEARCH_SIGNATURE)
        assert _canonical_types(restored) == _canonical_types(module._PREVIOUS_SEARCH_SIGNATURE)

    def test_0049_restores_exactly_the_shape_0048_published(self) -> None:
        assert _load_migration_module("0049")._search_schema(dated=False) == _load_migration_module("0048")._search_schema()

    def test_0065_frozen_schemas_match_their_hardcoded_signature_constant(self) -> None:
        module = _load_migration_module("0065")

        for sourced in (True, False):
            rendered = _bare_type_signature("api.search", module._search_schema(sourced=sourced))
            assert _canonical_types(rendered) == _canonical_types(module._SEARCH_SIGNATURE)

    def test_0065_restores_exactly_the_shape_0061_published(self) -> None:
        assert _load_migration_module("0065")._search_schema(sourced=False) == _load_migration_module("0061")._search_schema(ruled=True)

    def test_0065_restores_the_comment_0063_set(self) -> None:
        assert _load_migration_module("0065")._PREVIOUS_COMMENT_TEXT == _load_migration_module("0063")._COMMENT_TEXT

    def test_0066_frozen_schemas_match_their_hardcoded_signature_constant(self) -> None:
        module = _load_migration_module("0066")

        for page_dated in (True, False):
            rendered = _bare_type_signature("api.search", module._search_schema(page_dated=page_dated))
            assert _canonical_types(rendered) == _canonical_types(module._SEARCH_SIGNATURE)

    def test_0066_restores_exactly_the_shape_0065_published(self) -> None:
        assert _load_migration_module("0066")._search_schema(page_dated=False) == _load_migration_module("0065")._search_schema(sourced=True)

    def test_0066_restores_the_comment_0065_set(self) -> None:
        assert _load_migration_module("0066")._PREVIOUS_COMMENT_TEXT == _load_migration_module("0065")._COMMENT_TEXT

    def test_0070_frozen_schemas_match_their_hardcoded_signature_constant(self) -> None:
        module = _load_migration_module("0070")

        for documented in (True, False):
            rendered = _bare_type_signature("api.search", module._search_schema(documented=documented))
            assert _canonical_types(rendered) == _canonical_types(module._SEARCH_SIGNATURE)

    def test_0070_restores_exactly_the_shape_0066_published(self) -> None:
        assert _load_migration_module("0070")._search_schema(documented=False) == _load_migration_module("0066")._search_schema(page_dated=True)

    def test_0070_restores_the_comment_0066_set(self) -> None:
        assert _load_migration_module("0070")._PREVIOUS_COMMENT_TEXT == _load_migration_module("0066")._COMMENT_TEXT

    def test_0070_restores_the_table_comment_0061_set(self) -> None:
        assert _load_migration_module("0070")._PREVIOUS_TUNING_COMMENT == _load_migration_module("0061")._TUNING_COMMENT

    def test_0071_frozen_schemas_match_their_hardcoded_signature_constant(self) -> None:
        module = _load_migration_module("0071")

        for folded_snippets in (True, False):
            rendered = _bare_type_signature("api.search", module._search_schema(folded_snippets=folded_snippets))
            assert _canonical_types(rendered) == _canonical_types(module._SEARCH_SIGNATURE)

    def test_0071_restores_exactly_the_shape_0070_published(self) -> None:
        assert _load_migration_module("0071")._search_schema(folded_snippets=False) == _load_migration_module("0070")._search_schema(documented=True)

    def test_0071_publishes_the_shape_the_serving_layer_generates_from(self) -> None:
        assert _load_migration_module("0071")._search_schema(folded_snippets=True) == KB_SEARCH_SCHEMA

    def test_0071_restores_the_comments_and_the_check_0070_set(self) -> None:
        module, previous = _load_migration_module("0071"), _load_migration_module("0070")

        assert module._PREVIOUS_COMMENT_TEXT == previous._COMMENT_TEXT
        assert module._PREVIOUS_CAP_COMMENT == previous._CAP_COMMENT
        assert module._PREVIOUS_TUNING_COMMENT == previous._TUNING_COMMENT
        assert module._numbers_check(folded=False) == previous._numbers_check(capped=True)

    def test_0071_names_the_cap_function_it_creates_by_its_argument(self) -> None:
        module = _load_migration_module("0071")

        body = hits_per_document_body(module._search_schema(folded_snippets=True), function_name=module._CAP_FUNCTION)

        assert body.startswith(f"CREATE FUNCTION {module._CAP_FUNCTION}(collection text ")
        assert f"{module._CAP_FUNCTION}(text)" == module._CAP_SIGNATURE


class TestGraphSignatureDriftGuard:
    """0048's hand-written `api.graph_*` signature strings must match what its own frozen descriptors render.

    The traversal bodies are generated from a migration-local `GraphSchema`, while the `GRANT` /
    `DROP FUNCTION` / `COMMENT ON FUNCTION` strings beside that call are hand-written text. The
    currency argument is the drift risk this guards: it appears only because the descriptor names a
    currency column, so a descriptor and a signature string can disagree silently and leave a DROP
    naming a function that was never created. Both the published and the restored shapes are checked,
    since `downgrade` names its own pair.

    Pure Python — a frozen descriptor plus the real builder, no `CREATE FUNCTION`, no database — so
    this class runs even when Docker is unavailable.
    """

    def _schema(self, *, current: bool) -> GraphSchema:
        """0048's own frozen descriptor for one of the two shapes it publishes."""
        module = _load_migration_module("0048")
        schema: GraphSchema = module._graph_schema(edge_table="kb.edges", current=current)
        return schema

    def test_the_published_expand_signature_matches_the_currency_aware_descriptor(self) -> None:
        module = _load_migration_module("0048")
        rendered = CteGraphTraversalBuilder()._expand_signature(self._schema(current=True))

        assert _canonical_types(_bare_graph_signature("api.graph_expand", rendered)) == _canonical_types(module._EXPAND_SIGNATURE)

    def test_the_published_paths_signature_matches_the_currency_aware_descriptor(self) -> None:
        module = _load_migration_module("0048")
        rendered = CteGraphTraversalBuilder()._paths_signature(self._schema(current=True))

        assert _canonical_types(_bare_graph_signature("api.graph_paths", rendered)) == _canonical_types(module._PATHS_SIGNATURE)

    def test_the_frozen_descriptor_matches_the_one_the_serving_layer_generates_from(self) -> None:
        # The revision freezes its own copy so a later change never rewrites what it published. While
        # 0048 is the head, that copy is also what a database at head carries, so it has to be the
        # descriptor the serve layer reads against: a change here means a new revision is due.
        module = _load_migration_module("0048")

        assert module._graph_schema(edge_table="kb.edges", current=True) == kb_graph_schema(edge_table="kb.edges")

    def test_the_projection_reads_currency_from_a_relation_the_node_view_narrows_away(self) -> None:
        # `api.cards` serves only what the reading session may see, and the projection is written by
        # an index run announcing no collection, so reading currency from it would project every page
        # of a collection kept out of the shared library as current.
        module = _load_migration_module("0048")
        schema: GraphSchema = module._graph_schema(edge_table="kb.edges", current=True)
        refresh = AgeGraphTraversalBuilder("kb_graph").refresh_body(schema)

        assert schema.currency_source is not None
        assert schema.currency_source != schema.node_view
        assert f"LEFT JOIN {schema.currency_source} v" in refresh
        assert f"LEFT JOIN {schema.node_view} v" not in refresh

    def test_the_restored_signatures_match_the_currency_blind_descriptor(self) -> None:
        module = _load_migration_module("0048")
        builder = CteGraphTraversalBuilder()
        schema = self._schema(current=False)

        expand = _bare_graph_signature("api.graph_expand", builder._expand_signature(schema))
        paths = _bare_graph_signature("api.graph_paths", builder._paths_signature(schema))

        assert _canonical_types(expand) == _canonical_types(module._PREVIOUS_EXPAND_SIGNATURE)
        assert _canonical_types(paths) == _canonical_types(module._PREVIOUS_PATHS_SIGNATURE)


class TestRole:
    async def test_kb_reader_role_exists_and_is_read_only(self, conn: asyncpg.Connection):
        config = await conn.fetchval("SELECT rolconfig FROM pg_roles WHERE rolname = $1", _READER_ROLE)

        assert config is not None, f"{_READER_ROLE} role was not created"
        assert _has_setting(config, "default_transaction_read_only", "on")
        assert _has_setting(config, "statement_timeout", "10s")
        assert _has_setting(config, "lock_timeout", "5s")

    async def test_the_pre_rename_reader_role_is_gone(self, conn: asyncpg.Connection):
        stale = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'wiki_reader'")

        assert stale is None, "wiki_reader still exists — the rename left a second reader role behind"


class TestIdempotency:
    async def test_upgrade_from_head_is_a_noop(self, migrated: str):
        # Running upgrade head again on an already-migrated DB must not error or re-create objects.
        from engram.testing.infrastructure.knowledge_base_db import run_upgrade_head

        run_upgrade_head(migrated)  # second upgrade — alembic sees no pending revisions

        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            version = await connection.fetchval("SELECT version_num FROM alembic_version")
        finally:
            await connection.close()

        assert version == _chain_head()


def _alembic_config() -> Config:
    """An alembic `Config` pointed at this project's script directory, for reading the chain itself."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    return config


def _chain_head() -> str:
    """The migration chain's head revision, read from the scripts so a new revision never stales this test."""
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert head is not None, "alembic script directory has no head revision"
    return head


def _has_setting(config: Sequence[str], key: str, value: str) -> bool:
    """Whether `rolconfig` (a list of `key=value` strings) sets `key` to `value`."""
    return any(item.lower() == f"{key}={value}".lower() for item in config)


@pytest.fixture(params=["pg_search", "pg_textsearch"])
async def profile_conn(request: pytest.FixtureRequest, migrated: str) -> AsyncIterator[asyncpg.Connection]:
    r"""An asyncpg connection to a database migrated under the parametrized BM25 dialect.

    `pg_search` (the active regression) reuses the `migrated` fixture's throwaway
    container database, upgraded under the default dialect — the guard that the
    dialect-routed chain still produces today's schema locally. `pg_textsearch` (gated)
    runs the *same* assertions against the `pg_textsearch` database at
    :data:`_PG_TEXTSEARCH_URL_ENV`; it ``pytest.skip``\\ s unless that env var is set, so
    normal Docker-only runs skip it.

    `migrated` is taken as a direct fixture parameter (not `request.getfixturevalue`)
    so pytest-asyncio resolves the async `migrated` fixture on the test's running loop:
    resolving an *async* fixture lazily inside this *async* fixture body nests
    `Runner.run()` and raises "cannot be called from a running event loop".
    """
    dialect = request.param
    if dialect == "pg_search":
        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            yield connection
        finally:
            await connection.close()
        return

    pg_textsearch_url = os.environ.get(_PG_TEXTSEARCH_URL_ENV)
    if not pg_textsearch_url:
        pytest.skip(f"{_PG_TEXTSEARCH_URL_ENV} unset — skipping the gated pg_textsearch migration profile")
    async for connection in _migrated_pg_textsearch(pg_textsearch_url):
        yield connection


async def _migrated_pg_textsearch(admin_url: str) -> AsyncIterator[asyncpg.Connection]:
    """Run `alembic upgrade head` on the gated `pg_textsearch` database (`APP_RETRIEVAL__DIALECT=pg_textsearch`), then connect.

    The dialect is selected via the `APP_RETRIEVAL__DIALECT` env var that `alembic/env.py`
    reads (the same channel the cluster deployment uses); it is set for the duration of
    the upgrade and restored after. The cluster is external and persistent, so — unlike
    the per-test container database — this migrates the cluster in place and assumes a
    clean schema (`alembic upgrade head` is a no-op if already at head).
    """
    from engram.testing.infrastructure.knowledge_base_db import run_upgrade_head

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("APP_RETRIEVAL__DIALECT", "pg_textsearch")
        run_upgrade_head(admin_url)

    connection = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        yield connection
    finally:
        await connection.close()


class TestDialectProfiles:
    """The dialect-routed chain produces the BM25 + HNSW objects and a working `api.search` on each backend.

    Parametrized over `pg_search` (active — the local regression guard) and
    ``pg_textsearch`` (gated on :data:`_PG_TEXTSEARCH_URL_ENV`). Both run identical
    assertions, so the seam is proven to emit a functionally equivalent schema on
    either backend.
    """

    async def test_bm25_and_hnsw_indexes_exist(self, profile_conn: asyncpg.Connection):
        indexes = await _names(profile_conn, "SELECT indexname FROM pg_indexes WHERE schemaname = 'kb'")

        assert {"pages_bm25", "sections_bm25", "claims_bm25", "raw_chunks_bm25"} <= indexes
        assert {"pages_hnsw", "sections_hnsw", "claims_hnsw", "raw_hnsw"} <= indexes

    async def test_api_search_function_exists(self, profile_conn: asyncpg.Connection):
        functions = await _names(
            profile_conn, "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'api'"
        )

        assert "search" in functions

    async def test_api_search_smoke_call_returns_without_error(self, profile_conn: asyncpg.Connection):
        # An empty knowledge base returns zero rows; the value here is that the function PLANS and
        # EXECUTES under this dialect's BM25 grammar (the @@@/<@> operators resolve against the
        # right extension + indexes) — a parse/plan error would raise rather than return [].
        rows = await profile_conn.fetch("SELECT * FROM api.search($1, $2)", "diskann advanced filtering", 5)

        assert rows == []


@pytest.fixture(params=["cte", "age"])
async def graph_conn(request: pytest.FixtureRequest, migrated: str) -> AsyncIterator[asyncpg.Connection]:
    r"""An asyncpg connection to a database migrated under the parametrized graph backend.

    Mirrors :func:`profile_conn` for the graph seam 0004 adds. ``cte`` reuses the `migrated`
    fixture's container database — which holds the CTE traversal functions only when the bed
    migrated under :attr:`GraphBackend.CTE`, so the `cte` param `pytest.skip`\\ s unless
    `BED_GRAPH_BACKEND == 'cte'` (i.e. `KNOWLEDGE_BASE_TEST_DIALECT=pg_search`); on the default analog bed
    `migrated` is AGE-migrated, so the `cte` label would misrepresent what ran.
    `age` migrates a *fresh* database with :data:`_GRAPH_BACKEND_ENV` set to ``age`` (the channel
    ``alembic/env.py`` reads, exactly as :func:`_migrated_pg_textsearch` sets `APP_RETRIEVAL__DIALECT`),
    exercising the `CREATE EXTENSION age` + `create_graph` path; it prefers the local analog and
    falls back to the session `pg` container (the analog bed's container is itself AGE-capable).

    `migrated` is taken as a direct fixture parameter (not `request.getfixturevalue`)
    so pytest-asyncio resolves the async `migrated` fixture on the test's running loop:
    resolving an *async* fixture lazily inside this *async* fixture body nests
    `Runner.run()` and raises "cannot be called from a running event loop".

    ParadeDB does not ship Apache AGE, so the `age` parametrization `pytest.skip`\\ s
    when `CREATE EXTENSION age` is unavailable — the same graceful-skip posture the
    gated `pg_textsearch` profile uses.
    """
    backend = request.param
    if backend == "cte":
        if BED_GRAPH_BACKEND != "cte":
            pytest.skip(f"bed graph backend is {BED_GRAPH_BACKEND!r}, not cte — run with KNOWLEDGE_BASE_TEST_DIALECT=pg_search for the CTE walk")
        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            yield connection
        finally:
            await connection.close()
        return

    async for connection in _migrated_graph_age(request):
        yield connection


async def _migrated_graph_age(request: pytest.FixtureRequest) -> AsyncIterator[asyncpg.Connection]:
    """Migrate a fresh database under `APP_RETRIEVAL__GRAPH_BACKEND=age`, then connect; skip if AGE is absent.

    Prefers the local Postgres (an AGE + pg_textsearch image) when its admin URL is given via
    :data:`_ANALOG_URL_ENV` — the only configuration under which the ``age`` profile actually RUNS
    rather than skips, since ParadeDB ships no Apache AGE. Falls back to the session ParadeDB `pg`
    container (where `CREATE EXTENSION age` fails → clean skip), preserving the Docker-only default.
    Creates its own `test_<uuid>` database so the AGE projection never collides with the `cte`
    regression's schema; on the persistent analog it drops that database afterwards (the ephemeral
    container is discarded wholesale, so the pg path leaves cleanup to container teardown). The graph
    backend is forced to `age` for the upgrade; on the analog the BM25 dialect is also forced to
    `pg_textsearch` (the analog ships pg_textsearch, not ParadeDB's pg_search) — both via the `APP_*`
    env seam env.py reads.
    """
    from engram.testing.infrastructure.knowledge_base_db import run_upgrade_head

    # Prefer the local AGE-capable analog when configured; else the ParadeDB pg container (skip path).
    analog_url = os.environ.get(_ANALOG_URL_ENV)
    base_url = analog_url or request.getfixturevalue("pg")
    db_name = f"test_{uuid.uuid4().hex}"

    # CREATE DATABASE cannot run inside a transaction block; asyncpg autocommits simple execs.
    admin = await asyncpg.connect(_asyncpg_dsn(base_url))
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()

    # Swap only the database name in the SQLAlchemy URL, preserving the `+asyncpg` driver tag.
    base, _, _old = base_url.rpartition("/")
    target_url = f"{base}/{db_name}"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(_GRAPH_BACKEND_ENV, "age")
        if analog_url:
            mp.setenv("APP_RETRIEVAL__DIALECT", "pg_textsearch")  # the analog ships pg_textsearch, not ParadeDB's pg_search
        try:
            run_upgrade_head(target_url)
        except (asyncpg.PostgresError, sqlalchemy.exc.DBAPIError, OSError, RuntimeError) as exc:
            # `run_upgrade_head` drives alembic through SQLAlchemy, which wraps the asyncpg
            # `FeatureNotSupportedError` ("extension age is not available") in a DBAPIError —
            # so the wrapped form must be caught too, else the age case errors instead of skipping.
            # Since the AGE projection fork (revision 0026), a configured Apache AGE backend reaches
            # head like any other backend, so this branch now only fires when the extension itself
            # is unavailable (the ParadeDB `pg` fallback) or `graph_backend` is misconfigured — not
            # from a partitioning refusal, which no longer exists.
            pytest.skip(f"Apache AGE is unavailable on this bed — skipping the age graph profile ({exc})")

    connection = await asyncpg.connect(_asyncpg_dsn(target_url))
    try:
        yield connection
    finally:
        await connection.close()
        if analog_url:  # persistent server — drop the throwaway db (the ephemeral pg container is discarded wholesale)
            cleanup = await asyncpg.connect(_asyncpg_dsn(base_url))
            try:
                await cleanup.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            finally:
                await cleanup.close()


async def _graph_backend_of(conn: asyncpg.Connection) -> str:
    """Detect which graph backend a migrated DB was built under: `age` iff the AGE extension is installed.

    0004 only runs `CREATE EXTENSION age` + `create_graph('kb_graph')` under the `age` backend;
    the `cte` backend installs nothing. So the presence of the `age` extension is the backend
    signal the AGE-specific assertions gate on, rather than re-reading the env (restored after
    the upgrade).
    """
    installed = await _names(conn, "SELECT extname FROM pg_extension")
    return "age" if "age" in installed else "cte"


class TestGraphSurface:
    """0004_graph.py's `api.graph_expand` / `api.graph_paths`: existence, reader grants, AGE projection, smoke.

    Parametrized over `cte` (active local regression) and `age` (skips unless an
    AGE-capable image is configured), mirroring :class:`TestDialectProfiles`. Both run the
    backend-agnostic assertions (functions exist, `kb_reader` may EXECUTE, empty-knowledge_base
    smoke returns `[]`); the AGE-only assertions (extension + `kb_graph` graph) gate on the
    observed backend so they no-op under `cte`.
    """

    async def test_graph_functions_exist(self, graph_conn: asyncpg.Connection):
        functions = await _names(
            graph_conn, "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'api'"
        )

        assert functions >= EXPECTED_GRAPH_FUNCTIONS

    async def test_kb_reader_may_execute_graph_functions(self, graph_conn: asyncpg.Connection):
        # 0004 grants EXECUTE on both functions to the reader role explicitly (they are SECURITY
        # DEFINER and the reader sees api only) — assert the privilege per the GRANT signatures.
        can_expand = await graph_conn.fetchval("SELECT has_function_privilege($1, $2, 'EXECUTE')", _READER_ROLE, _EXPAND_SIGNATURE)
        can_paths = await graph_conn.fetchval("SELECT has_function_privilege($1, $2, 'EXECUTE')", _READER_ROLE, _PATHS_SIGNATURE)

        assert can_expand is True, f"{_READER_ROLE} was not granted EXECUTE on api.graph_expand"
        assert can_paths is True, f"{_READER_ROLE} was not granted EXECUTE on api.graph_paths"

    async def test_both_traversals_take_the_currency_argument_last(self, graph_conn: asyncpg.Connection):
        # 0048 appends `include_superseded boolean DEFAULT false` to both `api` functions, so a
        # caller asks for a superseded node rather than being told the graph has none.
        rows = await graph_conn.fetch(
            "SELECT p.proname, pg_get_function_arguments(p.oid) AS arguments"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'api' AND p.proname IN ('graph_expand', 'graph_paths')"
        )
        declared = sorted((row["proname"], row["arguments"]) for row in rows)

        assert [name for name, _ in declared] == ["graph_expand", "graph_paths"], "one overload of each, or a bare call is ambiguous"
        assert [name for name, arguments in declared if not arguments.endswith("include_superseded boolean DEFAULT false")] == []

    async def test_no_earlier_overload_survives_beside_the_published_traversal(self, graph_conn: asyncpg.Connection):
        # `to_regprocedure` resolves an exact argument list and answers NULL when nothing carries it,
        # so this names the five-argument functions (and the walk under them) that 0048 drops.
        gone = [
            signature
            for signature in (_PREVIOUS_EXPAND_SIGNATURE, _PREVIOUS_PATHS_SIGNATURE, _PREVIOUS_EXPAND_WALK_SIGNATURE)
            if await graph_conn.fetchval("SELECT to_regprocedure($1)", signature) is not None
        ]
        published = await graph_conn.fetchval("SELECT to_regprocedure($1)", _EXPAND_WALK_SIGNATURE)

        assert gone == []
        assert published is not None, "the six-argument walk the api wrapper calls is missing"

    async def test_age_extension_and_graph_present_on_age_backend(self, graph_conn: asyncpg.Connection):
        # AGE-only: under the cte backend 0004 projects nothing, so this assertion is not applicable.
        if await _graph_backend_of(graph_conn) != "age":
            pytest.skip("cte backend — no AGE extension or kb_graph graph is projected (not applicable)")

        extensions = await _names(graph_conn, "SELECT extname FROM pg_extension")
        assert "age" in extensions

        graphs = await _names(graph_conn, "SELECT name FROM ag_catalog.ag_graph")
        assert _AGE_GRAPH_NAME in graphs

    async def test_graph_expand_smoke_call_echoes_only_the_seed_without_error(self, graph_conn: asyncpg.Connection):
        # Empty/unseeded knowledge_base: the value is that graph_expand PLANS and EXECUTES under this
        # backend's traversal grammar (the CTE walk or the cypher() call resolve) — a parse/plan
        # error would raise. graph_expand always echoes the seed as a row (seeds included, per the
        # backend contract), so an unseeded seed yields exactly that one seed row: no neighbours,
        # NULL card columns (it is absent from kb.pages) and NULL edge columns (a seed is a
        # non-edge the handler skips). ARRAY[...]::text[] pins the seed arg type; p_collection is
        # the leading positional argument 0025 adds.
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, ARRAY['nonexistent']::text[])", "research")
        # The same call asking for superseded nodes too: 0048's trailing argument has to plan and
        # execute under both backends' grammar, not only parse.
        including_superseded = await graph_conn.fetch(
            "SELECT * FROM api.graph_expand($1, ARRAY['nonexistent']::text[], 2, NULL, 20, true)", "research"
        )

        assert [(row["id"], row["src_id"], row["rel"], row["type"]) for row in rows] == [("nonexistent", None, None, None)]
        assert [row["id"] for row in including_superseded] == ["nonexistent"]

    async def test_graph_paths_smoke_call_returns_empty_without_error(self, graph_conn: asyncpg.Connection):
        # Empty/unseeded knowledge_base: no path exists between two missing nodes, so graph_paths returns
        # [] — proving the function PLANS and EXECUTES under this backend without a parse/plan error.
        rows = await graph_conn.fetch("SELECT * FROM api.graph_paths($1, $2, $3)", "research", "a", "b")

        assert rows == []


@pytest.fixture
async def partitioned_url(pg: str) -> str:
    """A fresh database's admin URL, migrated to head — skips cleanly on a bed other than `cte`.

    Since the AGE projection fork (revision 0026), the default `pg_textsearch`/`age` bed reaches `head`
    too, but this fixture stays `cte`-only: the tests below exercise the identity migration's own
    mechanics (composite keys, foreign keys, the widened `api` signatures), which do not depend on
    which graph backend is configured — one bed proves them. The graph traversal's own per-collection
    isolation on Apache AGE is proven separately, by the AGE-bed cross-knowledge-base trap replay. Run with
    `KNOWLEDGE_BASE_TEST_DIALECT=pg_search` to exercise the tests below; on any other bed they skip here
    rather than error inside a later fixture or test body.
    """
    if BED_GRAPH_BACKEND != "cte":
        pytest.skip(f"bed graph backend is {BED_GRAPH_BACKEND!r}, not cte — run with KNOWLEDGE_BASE_TEST_DIALECT=pg_search for this identity-migration replay")
    target_url = await new_test_database(pg)
    run_upgrade(target_url, "head")
    return target_url


async def _insert_page(url: str, *, collection: str, page_id: str, card_text: str = "body", body_hash: str = "h") -> None:
    """Insert one minimal, NOT-NULL-satisfying `kb.pages` row directly (no ORM, no repository)."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        await connection.execute(
            "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash)"
            " VALUES ($1, $2, 'meta', $3, $3, 'verified', $4, $5)",
            collection,
            page_id,
            page_id,
            card_text,
            body_hash,
        )
    finally:
        await connection.close()


class TestKnowledgeBaseQualifiedIdentityMigration:
    """Revision 0025: the round-trip, its two loud guards, and the widened `api` read surface.

    The guard tests (`test_upgrade_guard_...` / `test_downgrade_guard_...`) fire during the plain
    Python backfill/duplicate checks, well before either revision touches graph-backend selection —
    they run identically under either graph backend and need no `partitioned_url` skip gate. Every
    other test here needs the database to actually reach `head`, so it takes `partitioned_url`.
    """

    async def test_upgrade_guard_fires_on_a_dangling_edge(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0024")

        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            # No page named 'nonexistent.page' or 'also.nonexistent' exists — `split_part(src_id,
            # '#', 1)` / `split_part(dst_id, '#', 1)` resolve to no row, exactly the dangling-edge
            # fixture the upgrade guard must catch before assigning either endpoint a collection.
            await connection.execute(
                "INSERT INTO memory.edges (src_id, src_kind, rel, dst_id, dst_kind)"
                " VALUES ('nonexistent.page', 'page', 'cites', 'also.nonexistent', 'page')"
            )
        finally:
            await connection.close()

        with pytest.raises(RuntimeError, match="edge endpoints resolve to no page"):
            run_upgrade(target_url, "0025")

    async def test_downgrade_guard_fires_on_a_cross_collection_duplicate(self, partitioned_url: str):
        # Legal only post-0025: two collections may now share a bare id — exactly the state
        # collapsing back to a single global id would silently drop one row of.
        await _insert_page(partitioned_url, collection="alpha", page_id="dup.page", card_text="alpha body", body_hash="ha")
        await _insert_page(partitioned_url, collection="beta", page_id="dup.page", card_text="beta body", body_hash="hb")

        with pytest.raises(RuntimeError, match="downgrading to global ids would drop rows"):
            run_downgrade(partitioned_url, "0024")

    async def test_round_trip_up_down_up(self, partitioned_url: str):
        run_downgrade(partitioned_url, "0024")
        run_upgrade(partitioned_url, "head")

        connection = await asyncpg.connect(_asyncpg_dsn(partitioned_url))
        try:
            version = await connection.fetchval("SELECT version_num FROM alembic_version")
            pk_columns = await connection.fetch(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
                " WHERE i.indrelid = 'kb.pages'::regclass AND i.indisprimary"
            )
        finally:
            await connection.close()

        assert version == _chain_head()
        assert {row["attname"] for row in pk_columns} == {"collection", "id"}

    async def test_every_widened_signature_answers_with_the_collection_argument(self, partitioned_url: str):
        await _insert_page(partitioned_url, collection="research", page_id="meta.x")

        connection = await asyncpg.connect(_asyncpg_dsn(partitioned_url))
        try:
            fetch_rows = await connection.fetch("SELECT * FROM api.fetch($1, $2)", "research", "meta.x")
            lookup_rows = await connection.fetch("SELECT * FROM api.lookup_value($1, $2)", "research", "facet:tasks")
            envelope_page_rows = await connection.fetch("SELECT * FROM api.envelope_page($1, $2)", "research", "meta.x")
            envelope_section_rows = await connection.fetch("SELECT * FROM api.envelope_section($1, $2)", "research", "meta.x#nope")
            graph_expand_rows = await connection.fetch(
                "SELECT * FROM api.graph_expand($1, ARRAY['meta.x']::text[])", "research"
            )
            graph_paths_rows = await connection.fetch("SELECT * FROM api.graph_paths($1, $2, $3)", "research", "meta.x", "meta.y")
        finally:
            await connection.close()

        assert len(fetch_rows) == 1
        assert lookup_rows == []  # no vocab rows seeded — proves the call plans/executes, not that facet:tasks has values
        assert len(envelope_page_rows) == 1
        assert envelope_section_rows == []  # 'meta.x#nope' names no section — proves the call plans/executes
        assert [row["id"] for row in graph_expand_rows] == ["meta.x"]  # the seed echoes back, unresolved neighbours
        assert graph_paths_rows == []  # no edge exists between the two ids

    async def test_sources_raw_has_the_collection_provider_provider_key_unique_constraint(self, partitioned_url: str):
        # One native item mirrored at most once per collection.
        connection = await asyncpg.connect(_asyncpg_dsn(partitioned_url))
        try:
            constraint_type = await connection.fetchval(
                "SELECT tc.constraint_type FROM information_schema.table_constraints tc"
                " WHERE tc.table_schema = 'kb' AND tc.table_name = 'sources_raw'"
                "   AND tc.constraint_name = 'sources_raw_collection_provider_provider_key_key'"
            )
        finally:
            await connection.close()

        assert constraint_type == "UNIQUE"

    async def test_fetch_of_an_id_that_exists_only_in_another_collection_returns_no_row(self, partitioned_url: str):
        await _insert_page(partitioned_url, collection="alpha", page_id="shared.id", card_text="alpha's own body")

        connection = await asyncpg.connect(_asyncpg_dsn(partitioned_url))
        try:
            own = await connection.fetch("SELECT * FROM api.fetch($1, $2)", "alpha", "shared.id")
            other = await connection.fetch("SELECT * FROM api.fetch($1, $2)", "beta", "shared.id")
        finally:
            await connection.close()

        assert len(own) == 1
        assert other == []


class TestAgeGraphProjectionForkMigration:
    """Revision 0026 on the bed it exists to unblock: the AGE default reaches `head`, and the round trip holds.

    Unlike `TestKnowledgeBaseQualifiedIdentityMigration` (whose `partitioned_url` fixture stays `cte`-only),
    this class needs the `age` bed specifically — it is the profile 0026 unblocks, so it skips on
    any other bed rather than gate on `cte`.
    """

    async def test_round_trip_up_down_up(self, pg: str):
        if BED_GRAPH_BACKEND != "age":
            pytest.skip(f"bed graph backend is {BED_GRAPH_BACKEND!r}, not age — 0026's round trip is proved on the bed it unblocks")

        target_url = await new_test_database(pg)
        run_upgrade(target_url, "head")

        run_downgrade(target_url, "0025")
        run_upgrade(target_url, "head")

        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            version = await connection.fetchval("SELECT version_num FROM alembic_version")
            functions = await _names(
                connection, "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'api'"
            )
        finally:
            await connection.close()

        assert version == _chain_head()
        assert functions >= EXPECTED_GRAPH_FUNCTIONS


class TestCollectionScopedOrientationMigration:
    """Revision 0027: the round trip, the two new scoped functions, and the collection-honest views."""

    async def test_round_trip_up_down_up(self, migrated: str):
        run_downgrade(migrated, "0026")
        run_upgrade(migrated, "head")

        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            version = await connection.fetchval("SELECT version_num FROM alembic_version")
            functions = await _names(
                connection, "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'api'"
            )
        finally:
            await connection.close()

        assert version == _chain_head()
        assert {"collection_schema_guide", "collection_authoring_rules"} <= functions

    async def test_collection_scoped_functions_answer_only_their_own_collection(self, migrated: str):
        await _insert_page(migrated, collection="research", page_id="meta.authoring@research", card_text="research authoring rules")
        await _insert_page(migrated, collection="other", page_id="meta.authoring@other", card_text="other authoring rules")

        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            await connection.execute(
                "INSERT INTO kb.vocab (collection, vocab, value, definition, meta)"
                " VALUES ($1, 'schema_guide', 'api.search', $2, '{}'::jsonb)",
                "research",
                "research's own purpose text",
            )
            await connection.execute(
                "INSERT INTO kb.vocab (collection, vocab, value, definition, meta)"
                " VALUES ($1, 'schema_guide', 'api.search', $2, '{}'::jsonb)",
                "other",
                "other's own purpose text",
            )

            research_authoring = await connection.fetchval("SELECT body FROM api.collection_authoring_rules('research')")
            other_authoring = await connection.fetchval("SELECT body FROM api.collection_authoring_rules('other')")
            research_guide = await connection.fetchval("SELECT purpose FROM api.collection_schema_guide('research')")
            other_guide = await connection.fetchval("SELECT purpose FROM api.collection_schema_guide('other')")
        finally:
            await connection.close()

        assert research_authoring == "research authoring rules"
        assert other_authoring == "other authoring rules"
        assert research_guide == "research's own purpose text"
        assert other_guide == "other's own purpose text"

    async def test_redefined_views_carry_the_collection_column_with_one_row_per_collection(self, migrated: str):
        await _insert_page(migrated, collection="research", page_id="meta.about@research", card_text="research about text")
        await _insert_page(migrated, collection="other", page_id="meta.about@other", card_text="other about text")

        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            rows = await connection.fetch("SELECT collection, body FROM api.about ORDER BY collection")
        finally:
            await connection.close()

        assert [(row["collection"], row["body"]) for row in rows] == [
            ("other", "other about text"),
            ("research", "research about text"),
        ]

    async def test_kb_reader_keeps_its_grants_after_the_redefinition(self, migrated: str):
        # DROP+CREATE loses grants unless the migration re-issues them; guard both surfaces.
        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            for view in ("about", "schema_guide", "authoring_rules"):
                can = await connection.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", _READER_ROLE, f"api.{view}")
                assert can is True, f"{_READER_ROLE} was not granted SELECT on api.{view}"
            for function in ("api.collection_schema_guide(text)", "api.collection_authoring_rules(text)"):
                can = await connection.fetchval("SELECT has_function_privilege($1, $2, 'EXECUTE')", _READER_ROLE, function)
                assert can is True, f"{_READER_ROLE} was not granted EXECUTE on {function}"
        finally:
            await connection.close()


class TestOrientationIdsAlwaysScopedMigration:
    """Revision 0042: a bare orientation row and every one of its dependents move to the scoped id.

    Seeds a bare `meta.about` page for one collection (pre-0042 state) plus one row in every table
    the migration repoints -- a section and a claim (FK-constrained) and a unit summary and an edge
    (FK-free, N-3) -- then upgrades to `head` and proves all five moved to `meta.about@<collection>`
    and the bare page row is gone, not merely that the migration runs against an empty database.
    """

    async def test_the_page_and_every_dependent_move_to_the_scoped_id(self, pg: str):
        collection = "scopetest"
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0041")

        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            await connection.execute(
                "INSERT INTO memory.pages (id, collection, kind, slug, title, status, card_text, body_hash)"
                " VALUES ('meta.about', $1, 'meta', 'about', 'About', 'verified', 'about body', 'h')",
                collection,
            )
            await connection.execute(
                "INSERT INTO memory.sections (id, collection, page_id, heading, path, ord, body, body_hash)"
                " VALUES ('meta.about#intro', $1, 'meta.about', 'Intro', 's1'::ltree, 0, 'section body', 'hs')",
                collection,
            )
            await connection.execute(
                "INSERT INTO memory.claims (id, collection, page_id, block_id, text, kind)"
                " VALUES ('meta.about#c1', $1, 'meta.about', 'c1', 'claim text', 'observation')",
                collection,
            )
            await connection.execute(
                "INSERT INTO memory.unit_summaries (collection, unit_id, unit_kind, summary, model, source_hash)"
                " VALUES ($1, 'meta.about', 'page', 'summary text', 'test-model', 'hu')",
                collection,
            )
            await connection.execute(
                "INSERT INTO memory.edges (src_collection, src_id, src_kind, rel, dst_collection, dst_id, dst_kind)"
                " VALUES ($1, 'meta.about', 'page', 'cites', $1, 'meta.about', 'page')",
                collection,
            )
        finally:
            await connection.close()

        run_upgrade(target_url, "head")

        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            page_ids = await connection.fetch("SELECT id FROM kb.pages WHERE collection = $1", collection)
            section_page_ids = await connection.fetch("SELECT page_id FROM kb.sections WHERE collection = $1", collection)
            claim_page_ids = await connection.fetch("SELECT page_id FROM kb.claims WHERE collection = $1", collection)
            summary_unit_ids = await connection.fetch("SELECT unit_id FROM kb.unit_summaries WHERE collection = $1", collection)
            edges = await connection.fetch("SELECT src_id, dst_id FROM kb.edges WHERE src_collection = $1", collection)
        finally:
            await connection.close()

        scoped = "meta.about@scopetest"
        assert {row["id"] for row in page_ids} == {scoped}, "the bare row must be gone, only the scoped clone remains"
        assert {row["page_id"] for row in section_page_ids} == {scoped}
        assert {row["page_id"] for row in claim_page_ids} == {scoped}
        assert {row["unit_id"] for row in summary_unit_ids} == {scoped}
        assert [(row["src_id"], row["dst_id"]) for row in edges] == [(scoped, scoped)]


# The two `api` signatures the 0046 comment test pins, one per path `_regenerate_functions` takes.
# `envelope_collection_kinds` publishes a renamed `kind` column, so CREATE OR REPLACE refuses its new
# row type and the function is dropped and created fresh under its new name (a fresh oid), which is
# what takes its COMMENT with it. `log_session` only names a renamed schema in its body, so it is
# replaced in place and keeps its oid, and its comment is never at risk.
_DROPPED_FUNCTION_BEFORE_0046 = "api.envelope_collection_kinds(text,text[])"
_DROPPED_FUNCTION_AFTER_0046 = "api.envelope_collection_page_types(text,text[])"
_REPLACED_FUNCTION_0046 = "api.log_session(text,text,text[],text[],integer,text)"


async def _function_identity(url: str, signature: str) -> tuple[int | None, str | None]:
    """One function's oid and its own COMMENT, by signature; both None when the cluster carries no such function."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        row = await connection.fetchrow(
            "SELECT p.oid::bigint AS oid, obj_description(p.oid, 'pg_proc') AS note FROM pg_proc p WHERE p.oid = to_regprocedure($1)::oid",
            signature,
        )
    finally:
        await connection.close()
    if row is None:
        return (None, None)
    return (row["oid"], row["note"])


async def _refresh_jobs(url: str) -> list[tuple[str, str, str]]:
    """Every `coordination.jobs` row under any of the three refresh-job kinds, as (kind, key, state), ordered.

    The kind is `corpus.ingest` before 0046, `knowledge_base.ingest` between 0046 and 0047, and
    `kb.refresh` after it, so one query reads every point in the chain these tests stop at.
    """
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch(
            "SELECT kind, key, state FROM coordination.jobs WHERE kind IN ('corpus.ingest', 'knowledge_base.ingest', 'kb.refresh')"
            " ORDER BY kind, key, state"
        )
    finally:
        await connection.close()
    return [(row["kind"], row["key"], row["state"]) for row in rows]


class TestKnowledgeBaseNamesMigration:
    """Revision 0046: the comment a regenerated `api` function keeps, and the ingest jobs that change kind."""

    async def test_a_regenerated_function_keeps_its_comment_on_both_paths(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0045")

        dropped_oid_before, dropped_note_before = await _function_identity(target_url, _DROPPED_FUNCTION_BEFORE_0046)
        replaced_oid_before, replaced_note_before = await _function_identity(target_url, _REPLACED_FUNCTION_0046)

        run_upgrade(target_url, "head")

        dropped_oid_after, dropped_note_after = await _function_identity(target_url, _DROPPED_FUNCTION_AFTER_0046)
        replaced_oid_after, replaced_note_after = await _function_identity(target_url, _REPLACED_FUNCTION_0046)

        assert dropped_note_before is not None
        assert "envelope_collection_kinds" in dropped_note_before
        assert replaced_note_before is not None
        assert "memory.query_log" in replaced_note_before
        # A fresh oid is the drop-and-create path; the same oid is CREATE OR REPLACE. `api_objects`
        # (engram/retrieval/infrastructure/gateway.py) serves both comments, so neither path may lose one.
        assert dropped_oid_after is not None
        assert dropped_oid_after != dropped_oid_before
        assert replaced_oid_after == replaced_oid_before
        assert dropped_note_after is not None, f"{_DROPPED_FUNCTION_AFTER_0046} lost the comment the drop took"
        assert "api.envelope_collection_page_types(" in dropped_note_after
        assert "envelope_collection_kinds" not in dropped_note_after
        assert replaced_note_after is not None, f"{_REPLACED_FUNCTION_0046} lost its comment"
        assert "kb.query_log" in replaced_note_after
        assert "memory.query_log" not in replaced_note_after

    async def test_ingest_jobs_change_kind_except_the_row_that_would_collide_on_the_live_key(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0045")

        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            # `ux_jobs_live_key` (0034) is unique on (kind, key) for a live, keyed job. The renamed
            # code writes `knowledge_base.ingest`, so 'beta' already holds a live row under the new
            # kind: moving the old live 'beta' row onto it would raise and roll the migration back.
            await connection.executemany(
                "INSERT INTO coordination.jobs (kind, key, state) VALUES ($1, $2, $3)",
                [
                    ("corpus.ingest", "alpha", "queued"),
                    ("corpus.ingest", "", "running"),
                    ("corpus.ingest", "gamma", "done"),
                    ("corpus.ingest", "beta", "queued"),
                    ("corpus.ingest", "beta", "done"),
                    ("knowledge_base.ingest", "beta", "running"),
                ],
            )
        finally:
            await connection.close()

        run_upgrade(target_url, "0046")

        assert await _refresh_jobs(target_url) == [
            ("corpus.ingest", "beta", "queued"),
            ("knowledge_base.ingest", "", "running"),
            ("knowledge_base.ingest", "alpha", "queued"),
            ("knowledge_base.ingest", "beta", "done"),
            ("knowledge_base.ingest", "beta", "running"),
            ("knowledge_base.ingest", "gamma", "done"),
        ]


# The three switch shapes 0047 inverts: the collection that opted out of the library, one that said
# it stayed in, and one that never carried the key. A fourth name is never indexed at all, so it has
# no fingerprint row for either spelling to be read from.
_OPTED_OUT_COLLECTION = "privacy-opted-out"
_IN_THE_LIBRARY_COLLECTION = "privacy-in-the-library"
_NO_SWITCH_COLLECTION = "privacy-no-switch"
_UNINDEXED_COLLECTION = "privacy-never-indexed"


async def _privacy_switches(url: str) -> list[tuple[str, str | None, str | None, str | None]]:
    """Every fingerprint row as (collection, library, private, embeddings), ordered, each switch absent as None."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch(
            "SELECT collection, switches ->> 'library' AS library, switches ->> 'private' AS private,"
            " switches ->> 'embeddings' AS embeddings FROM kb.index_fingerprint ORDER BY collection"
        )
    finally:
        await connection.close()
    return [(row["collection"], row["library"], row["private"], row["embeddings"]) for row in rows]


async def _collection_visibility(url: str) -> dict[str, bool]:
    """What `api.visible_collection` answers for each of the four names, to the anonymous reader."""
    names = (_OPTED_OUT_COLLECTION, _IN_THE_LIBRARY_COLLECTION, _NO_SWITCH_COLLECTION, _UNINDEXED_COLLECTION)
    connection = await asyncpg.connect(_asyncpg_dsn(reader_url_for(url)))
    try:
        answers = {name: await connection.fetchval("SELECT api.visible_collection($1)", name) for name in names}
    finally:
        await connection.close()
    return answers


class TestPrivateSwitchAndRefreshJobMigration:
    """Revision 0047: the privacy switch inverts under `api.visible_collection`, and a queued refresh changes kind."""

    async def test_the_privacy_switch_inverts_and_the_downgrade_puts_every_shape_back(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0046")

        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            await connection.executemany(
                "INSERT INTO kb.index_fingerprint (collection, indexer_version, spec_hash, vocabulary_hash, embed_model, switches)"
                " VALUES ($1, 'v1', 'spec', 'vocab', 'embed', $2::jsonb)",
                [
                    (_OPTED_OUT_COLLECTION, '{"embeddings": true, "library": false}'),
                    (_IN_THE_LIBRARY_COLLECTION, '{"embeddings": true, "library": true}'),
                    (_NO_SWITCH_COLLECTION, '{"embeddings": true}'),
                ],
            )
        finally:
            await connection.close()

        visible_before = {_OPTED_OUT_COLLECTION: False, _IN_THE_LIBRARY_COLLECTION: True}
        visible_before |= {_NO_SWITCH_COLLECTION: True, _UNINDEXED_COLLECTION: True}
        assert await _collection_visibility(target_url) == visible_before, "the pre-0047 function reads `library: false`"

        run_upgrade(target_url, "head")

        assert await _privacy_switches(target_url) == [
            (_IN_THE_LIBRARY_COLLECTION, None, None, "true"),
            (_NO_SWITCH_COLLECTION, None, None, "true"),
            (_OPTED_OUT_COLLECTION, None, "true", "true"),
        ], "the opted-out row gains `private: true`, the other two lose the old key and gain nothing"
        assert await _collection_visibility(target_url) == visible_before, "the same collection is private, read off the new key"

        run_downgrade(target_url, "0046")

        assert await _privacy_switches(target_url) == [
            # `library: true` and no `library` key at all both mean shared, so the row that said it
            # stayed in comes back as the row that never spoke, which the function reads the same way.
            (_IN_THE_LIBRARY_COLLECTION, None, None, "true"),
            (_NO_SWITCH_COLLECTION, None, None, "true"),
            (_OPTED_OUT_COLLECTION, "false", None, "true"),
        ]
        assert await _collection_visibility(target_url) == visible_before, "the mirror leaves every collection as private as it was"

    async def test_refresh_jobs_change_kind_except_the_row_that_would_collide_on_the_live_key(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0046")

        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            # `ux_jobs_live_key` (0034) is unique on (kind, key) for a live, keyed job. The renamed
            # code writes `kb.refresh`, so 'beta' already holds a live row under the new kind:
            # moving the old live 'beta' row onto it would raise and roll the migration back.
            await connection.executemany(
                "INSERT INTO coordination.jobs (kind, key, state) VALUES ($1, $2, $3)",
                [
                    ("knowledge_base.ingest", "", "running"),
                    ("knowledge_base.ingest", "alpha", "queued"),
                    ("knowledge_base.ingest", "beta", "queued"),
                    ("knowledge_base.ingest", "beta", "done"),
                    ("kb.refresh", "beta", "running"),
                ],
            )
        finally:
            await connection.close()

        run_upgrade(target_url, "head")

        assert await _refresh_jobs(target_url) == [
            ("kb.refresh", "", "running"),
            ("kb.refresh", "alpha", "queued"),
            ("kb.refresh", "beta", "done"),
            ("kb.refresh", "beta", "running"),
            ("knowledge_base.ingest", "beta", "queued"),
        ]


async def _function_exists(url: str, signature: str) -> bool:
    """Whether the database carries a function with exactly this argument list.

    `to_regprocedure` resolves an exact argument list and answers NULL when nothing carries it, so
    this distinguishes one overload from another rather than only naming the function.
    """
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return await connection.fetchval("SELECT to_regprocedure($1)", signature) is not None
    finally:
        await connection.close()


async def _reader_may_execute(url: str, signature: str) -> bool:
    """Whether `kb_reader` holds EXECUTE on exactly this signature (a drop takes the grant with it)."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        granted = await connection.fetchval("SELECT has_function_privilege($1, $2, 'EXECUTE')", _READER_ROLE, signature)
    finally:
        await connection.close()
    return granted is True


async def _relation_exists(url: str, name: str) -> bool:
    """Whether a table or view of that qualified name exists."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return await connection.fetchval("SELECT to_regclass($1)", name) is not None
    finally:
        await connection.close()


async def _view_comment(url: str, name: str) -> str | None:
    """The comment on a view, by qualified name."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return await connection.fetchval("SELECT obj_description(to_regclass($1), 'pg_class')", name)
    finally:
        await connection.close()


async def _reader_may_select(url: str, name: str) -> bool:
    """Whether the read-only role may select from a relation."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return await connection.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", _READER_ROLE, name)
    finally:
        await connection.close()


async def _column_names(url: str, name: str) -> set[str]:
    """The column names of a table or view, by qualified name."""
    schema, relation = name.split(".")
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2", schema, relation
        )
    finally:
        await connection.close()
    return {row["column_name"] for row in rows}


async def _function_comment(url: str, *, schema: str, name: str) -> str | None:
    """The comment on the one `<schema>.<name>` function, or None when it carries none."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return await connection.fetchval(
            "SELECT obj_description(p.oid, 'pg_proc')"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = $1 AND p.proname = $2",
            schema,
            name,
        )
    finally:
        await connection.close()


class TestFocusedRetrievalResultsMigration:
    """Revision 0048: `api.search`'s per-leg facts and the traversal's currency argument, up and back down."""

    def test_the_revision_sits_directly_on_0047(self) -> None:
        # The chain's own linkage, read off the file rather than a literal head, so a later revision
        # extending the chain does not stale this: what matters is that 0048 follows 0047.
        module = _load_migration_module("0048")

        assert module.revision == "0048"
        assert module.down_revision == "0047"
        assert "0048" in {script.revision for script in ScriptDirectory.from_config(_alembic_config()).walk_revisions()}

    async def test_the_traversal_gains_the_currency_argument_and_the_downgrade_takes_it_back(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0047")

        assert await _function_exists(target_url, _PREVIOUS_EXPAND_SIGNATURE), "0047 carries the five-argument traversal"
        assert await _function_exists(target_url, _PREVIOUS_PATHS_SIGNATURE)
        assert not await _function_exists(target_url, _EXPAND_SIGNATURE)

        run_upgrade(target_url, "head")

        assert await _function_exists(target_url, _EXPAND_SIGNATURE)
        assert await _function_exists(target_url, _PATHS_SIGNATURE)
        assert not await _function_exists(target_url, _PREVIOUS_EXPAND_SIGNATURE), "the replaced overload is dropped, never left beside"
        assert not await _function_exists(target_url, _PREVIOUS_PATHS_SIGNATURE)
        assert await _reader_may_execute(target_url, _EXPAND_SIGNATURE), "the grant is re-granted on the published signature"
        assert await _reader_may_execute(target_url, _PATHS_SIGNATURE)
        assert "include_superseded" in (await _function_comment(target_url, schema="api", name="graph_expand") or "")
        assert "include_superseded" in (await _function_comment(target_url, schema="api", name="graph_paths") or "")

        run_downgrade(target_url, "0047")

        assert await _function_exists(target_url, _PREVIOUS_EXPAND_SIGNATURE), "the downgrade puts the five-argument traversal back"
        assert await _function_exists(target_url, _PREVIOUS_PATHS_SIGNATURE)
        assert not await _function_exists(target_url, _EXPAND_SIGNATURE)
        assert not await _function_exists(target_url, _EXPAND_WALK_SIGNATURE)
        assert await _reader_may_execute(target_url, _PREVIOUS_EXPAND_SIGNATURE), "the restored signature carries the grant again"
        assert "include_superseded" not in (await _function_comment(target_url, schema="api", name="graph_expand") or "")

        run_upgrade(target_url, "head")

        assert await _function_exists(target_url, _EXPAND_SIGNATURE), "the chain replays after a downgrade"

    async def test_the_projection_comment_names_the_relations_it_reads_and_the_downgrade_restores_the_old_one(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0047")

        before = await _function_comment(target_url, schema="kb", name="refresh_graph")
        assert before is not None

        run_upgrade(target_url, "head")

        after = await _function_comment(target_url, schema="kb", name="refresh_graph")
        assert after is not None
        assert "memory.edges" not in after, "the relation was renamed at 0046; the comment follows it here"
        assert "kb.edges" in after
        assert "kb.pages" in after, "a reader of this comment learns where a vertex's currency is read from"

        run_downgrade(target_url, "0047")

        assert await _function_comment(target_url, schema="kb", name="refresh_graph") == before

    async def test_the_search_comment_names_the_new_columns_and_the_downgrade_restores_the_old_one(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0047")

        before = await _function_comment(target_url, schema="api", name="search")
        assert before is not None
        assert "lexical_rank" not in before, "0047's comment stops at the fused score"

        run_upgrade(target_url, "0048")

        after = await _function_comment(target_url, schema="api", name="search")
        assert after is not None
        assert [column for column in _SEARCH_RESULT_FACTS if column not in after] == []
        assert await _reader_may_execute(target_url, "api.search(text, integer, halfvec, text, jsonb, text, boolean)")

        run_downgrade(target_url, "0047")

        assert await _function_comment(target_url, schema="api", name="search") == before, "the downgrade restores the comment byte for byte"
        assert await _reader_may_execute(target_url, "api.search(text, integer, halfvec, text, jsonb, text, boolean)")


class TestSearchDateRangeMigration:
    """Revision 0049: `api.search` takes `since` and `until`, up and back down."""

    _DATED = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date)"
    _UNDATED = "api.search(text, integer, halfvec, text, jsonb, text, boolean)"

    def test_the_revision_sits_directly_on_0048(self) -> None:
        module = _load_migration_module("0049")

        assert module.revision == "0049"
        assert module.down_revision == "0048"
        assert "0049" in {script.revision for script in ScriptDirectory.from_config(_alembic_config()).walk_revisions()}

    async def test_the_range_replaces_the_seven_argument_search_and_the_downgrade_puts_it_back(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0048")
        before = await _function_comment(target_url, schema="api", name="search")

        run_upgrade(target_url, "0049")

        after = await _function_comment(target_url, schema="api", name="search")
        assert await _function_exists(target_url, self._DATED)
        assert not await _function_exists(target_url, self._UNDATED), "no seven-argument overload is left for a call to resolve against"
        assert await _reader_may_execute(target_url, self._DATED)
        assert after is not None
        assert "'since' and 'until' are dates" in after

        run_downgrade(target_url, "0048")

        assert await _function_exists(target_url, self._UNDATED)
        assert not await _function_exists(target_url, self._DATED)
        assert await _reader_may_execute(target_url, self._UNDATED)
        assert await _function_comment(target_url, schema="api", name="search") == before, "the downgrade restores the comment byte for byte"


class TestDropComparisonTablesMigration:
    """Revision 0050: the comparison table and its view go, and come back on the way down."""

    async def test_the_table_and_the_view_go_and_come_back_exactly(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0049")
        comment = await _view_comment(target_url, "api.matrix")

        run_upgrade(target_url, "0050")

        assert not await _relation_exists(target_url, "kb.comparison_cells")
        assert not await _relation_exists(target_url, "api.matrix")

        run_downgrade(target_url, "0049")

        assert await _relation_exists(target_url, "kb.comparison_cells")
        assert await _view_comment(target_url, "api.matrix") == comment
        assert await _reader_may_select(target_url, "api.matrix")

    async def test_a_table_that_still_holds_rows_is_refused(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0049")
        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            for page_id in ("cmp.a", "ent.b"):
                await connection.execute(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash)"
                    " VALUES ($1, 'c', 'note', $1, $1, 'draft', $1, $1)",
                    page_id,
                )
            await connection.execute(
                "INSERT INTO kb.comparison_cells (collection, comparison_id, row_entity_collection, row_entity, dimension, value)"
                " VALUES ('c', 'cmp.a', 'c', 'ent.b', 'speed', 'fast')"
            )
        finally:
            await connection.close()

        with pytest.raises(RuntimeError, match=r"kb\.comparison_cells holds 1 row"):
            run_upgrade(target_url, "0050")

        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == "0049"
            assert await connection.fetchval("SELECT count(*) FROM kb.comparison_cells") == 1
        finally:
            await connection.close()
        assert await _relation_exists(target_url, "api.matrix")


class TestDropResearchViewsMigration:
    """Revision 0052: the research views and the research columns of cards and claims go, and come back on the way down."""

    async def test_the_research_views_go_and_come_back(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0051")
        comments = {view: await _view_comment(target_url, view) for view in _RESEARCH_VIEWS}
        card_columns = await _column_names(target_url, "api.cards")
        claim_columns = await _column_names(target_url, "api.claims")
        every_comment = await _every_api_comment(target_url)

        run_upgrade(target_url, "0052")

        for view in _RESEARCH_VIEWS:
            assert not await _relation_exists(target_url, view)
        assert await _column_names(target_url, "api.claims") >= {"page_id", "page_title"}
        assert not await _column_names(target_url, "api.claims") & {"paper_id", "paper_title", "paper_year"}
        assert not await _column_names(target_url, "api.cards") & {"paper_type", "maturity", "entity_type", "year", "tasks", "domains", "stages"}
        assert await _reader_may_select(target_url, "api.cards")
        assert await _reader_may_select(target_url, "api.claims")

        run_downgrade(target_url, "0051")

        for view in _RESEARCH_VIEWS:
            assert await _reader_may_select(target_url, view)
            assert await _view_comment(target_url, view) == comments[view]
        assert await _column_names(target_url, "api.cards") == card_columns
        assert await _column_names(target_url, "api.claims") == claim_columns
        assert await _every_api_comment(target_url) == every_comment


_RESEARCH_VIEWS = ("api.papers", "api.scoreboard", "api.entities", "api.coverage")


async def _every_api_comment(url: str) -> list[tuple[str, str | None]]:
    """Every comment on an `api` relation, column or function, in a stable order."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch(
            "SELECT c.relname || coalesce('.' || a.attname, '') AS name, coalesce(col_description(c.oid, a.attnum), obj_description(c.oid, 'pg_class'))"
            " AS comment FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped AND c.relkind = 'v'"
            " WHERE n.nspname = 'api' AND c.relkind IN ('v', 'r')"
            " UNION ALL SELECT p.oid::regprocedure::text, obj_description(p.oid, 'pg_proc') FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'api'"
            " UNION ALL SELECT c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'api' AND c.relkind = 'v'"
            " ORDER BY 1, 2"
        )
    finally:
        await connection.close()
    return [(row["name"], row["comment"]) for row in rows]


class TestNeutralCommentsMigration:
    """Revision 0053: every rewritten comment goes neutral, and comes back exactly on the way down."""

    async def test_every_comment_comes_back_exactly(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0052")
        before = await _every_api_comment(target_url)

        run_upgrade(target_url, "0053")

        assert await _every_api_comment(target_url) != before
        assert await _view_comment(target_url, "api.cards") == (
            "L1 card scan (~50-150 tokens/row): one compact row per page across every page type. Filter by type, exclude "
            "stubs/superseded, order by updated. Example: SELECT id, title, summary FROM api.cards WHERE type = '<page-type>' "
            "AND NOT superseded LIMIT 20;"
        )

        run_downgrade(target_url, "0052")

        assert await _every_api_comment(target_url) == before


# The four typed claim columns 0064 folds into `kb.claims.fields`.
_TYPED_CLAIM_COLUMNS = ("condition", "baseline", "magnitude", "eval_setting")


class TestClaimFieldsMigration:
    """Revision 0064: the typed claim columns move into `kb.claims.fields`, and come back exactly on the way down."""

    def test_the_revision_sits_directly_on_0063(self) -> None:
        module = _load_migration_module("0064")

        assert module.revision == "0064"
        assert module.down_revision == "0063"

    async def test_the_columns_move_into_fields_and_come_back(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0063")
        table_columns = await _column_names(target_url, "kb.claims")
        claim_columns = await _column_names(target_url, "api.claims")
        contradiction_columns = await _column_names(target_url, "api.contradictions")
        every_comment = await _every_api_comment(target_url)
        for page_id in ("doc.a", "ent.subject", "ent.first-target", "ent.second-target"):
            await _insert_page(target_url, collection="c", page_id=page_id)
        await _execute(
            target_url,
            "INSERT INTO kb.claims (collection, id, page_id, block_id, text, kind, subject_collection, subject_id, status,"
            " condition, baseline, magnitude, eval_setting) VALUES"
            " ('c', 'doc.a#c1', 'doc.a', 'c1', 'first', 'observation', 'c', 'ent.subject', 'disputed', 'zero-shot', NULL, '+4 points', NULL),"
            " ('c', 'doc.a#c2', 'doc.a', 'c2', 'second', 'observation', 'c', 'ent.subject', 'disputed', NULL, 'the old method', NULL, 'closed-book'),"
            " ('c', 'doc.a#c3', 'doc.a', 'c3', 'third', 'observation', NULL, NULL, 'active', NULL, NULL, NULL, NULL)",
            "INSERT INTO kb.claim_qualifiers (collection, claim_id, qualifier, ord, target_collection, target_id) VALUES"
            " ('c', 'doc.a#c1', 'applies_to', 1, 'c', 'ent.second-target'), ('c', 'doc.a#c1', 'applies_to', 0, 'c', 'ent.first-target'),"
            " ('c', 'doc.a#c1', 'governed_by', 0, 'c', 'ent.subject')",
        )

        run_upgrade(target_url, "0064")

        assert not await _column_names(target_url, "kb.claims") & set(_TYPED_CLAIM_COLUMNS)
        assert await _json_by_id(target_url, "SELECT id, fields::text FROM kb.claims") == {
            "doc.a#c1": {"condition": "zero-shot", "magnitude": "+4 points"},
            "doc.a#c2": {"baseline": "the old method", "eval_setting": "closed-book"},
            "doc.a#c3": {},
        }
        assert await _column_names(target_url, "api.claims") == (claim_columns - {*_RESEARCH_CLAIM_VIEW_COLUMNS}) | {"fields", "links"}
        assert await _json_by_id(target_url, "SELECT claim_id, links::text FROM api.claims") == {
            "doc.a#c1": {"applies_to": ["ent.first-target", "ent.second-target"], "governed_by": ["ent.subject"]},
            "doc.a#c2": {},
            "doc.a#c3": {},
        }
        assert await _json_by_id(target_url, "SELECT claim_id, fields::text FROM api.claims WHERE claim_id = 'doc.a#c1'") == {
            "doc.a#c1": {"condition": "zero-shot", "magnitude": "+4 points"}
        }
        assert await _column_names(target_url, "api.contradictions") == (contradiction_columns - {"condition_a", "condition_b"}) | {
            "fields_a",
            "fields_b",
        }
        assert await _json_by_id(
            target_url, "SELECT claim_a || ' ' || claim_b, json_build_array(fields_a, fields_b)::text FROM api.contradictions"
        ) == {
            "doc.a#c1 doc.a#c2": [
                {"condition": "zero-shot", "magnitude": "+4 points"},
                {"baseline": "the old method", "eval_setting": "closed-book"},
            ]
        }
        for view in ("api.claims", "api.contradictions"):
            assert await _reader_may_select(target_url, view)
            assert await _uncommented_columns(target_url, view) == set()

        run_downgrade(target_url, "0063")

        assert await _column_names(target_url, "kb.claims") == table_columns
        assert await _rows(target_url, "SELECT id, condition, baseline, magnitude, eval_setting FROM kb.claims ORDER BY id") == [
            ("doc.a#c1", "zero-shot", None, "+4 points", None),
            ("doc.a#c2", None, "the old method", None, "closed-book"),
            ("doc.a#c3", None, None, None, None),
        ]
        assert await _column_names(target_url, "api.claims") == claim_columns
        assert await _column_names(target_url, "api.contradictions") == contradiction_columns
        assert await _every_api_comment(target_url) == every_comment
        for view in ("api.claims", "api.contradictions"):
            assert await _reader_may_select(target_url, view)


# The `api.claims` columns at 0063 that named one knowledge base's claim fields.
_RESEARCH_CLAIM_VIEW_COLUMNS = ("dataset_name", "task_name", "metric_name", "condition", "magnitude", "eval_setting")


async def _execute(url: str, *statements: str) -> None:
    """Run each statement in order on one admin connection."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        for statement in statements:
            await connection.execute(statement)
    finally:
        await connection.close()


async def _rows(url: str, query: str) -> list[tuple[str | None, ...]]:
    """Every row of a query whose columns are all text, as a plain tuple."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return [tuple(None if value is None else str(value) for value in row) for row in await connection.fetch(query)]
    finally:
        await connection.close()


async def _json_by_id(url: str, query: str) -> dict[str, JsonValue]:
    """A two-column query's rows as `{first column: second column decoded from JSON text}`."""
    return {str(key): json.loads(str(value)) for key, value in await _rows(url, query)}


async def _uncommented_columns(url: str, name: str) -> set[str]:
    """The columns of a view, by qualified name, that carry no comment."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass($1) AND attnum > 0 AND NOT attisdropped"
            " AND col_description(attrelid, attnum) IS NULL",
            name,
        )
    finally:
        await connection.close()
    return {row["attname"] for row in rows}


async def _column_comments(url: str, name: str) -> dict[str, str | None]:
    """Each column of a table or view with its comment, in column order, by qualified name."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch(
            "SELECT a.attname AS name, col_description(a.attrelid, a.attnum) AS comment FROM pg_attribute a"
            " WHERE a.attrelid = to_regclass($1) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
            name,
        )
    finally:
        await connection.close()
    return {row["name"]: row["comment"] for row in rows}


async def _view_definition(url: str, name: str) -> str:
    """The SQL a view runs, by qualified name."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return await connection.fetchval("SELECT pg_get_viewdef(to_regclass($1))", name)
    finally:
        await connection.close()


class TestRawChunkReadingOrderMigration:
    """Revision 0068: `api.raw_chunks` serves each chunk's `seq` and its comment reads a mirror in that order."""

    def test_the_revision_sits_directly_on_0067(self) -> None:
        module = _load_migration_module("0068")

        assert module.revision == "0068"
        assert module.down_revision == "0067"

    async def test_the_view_serves_seq_and_the_downgrade_puts_it_back_exactly(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0067")
        columns_before = await _column_comments(target_url, "api.raw_chunks")
        comment_before = await _view_comment(target_url, "api.raw_chunks")
        definition_before = await _view_definition(target_url, "api.raw_chunks")

        run_upgrade(target_url, "0068")

        columns_after = await _column_comments(target_url, "api.raw_chunks")
        assert list(columns_after) == [*columns_before, "seq"]
        assert columns_after["seq"]
        comment = await _view_comment(target_url, "api.raw_chunks")
        assert comment is not None
        assert "ORDER BY seq" in comment
        assert "chunk sizes" in comment
        assert "numbered headings" not in comment
        assert "visible_collection" in await _view_definition(target_url, "api.raw_chunks")
        assert await _reader_may_select(target_url, "api.raw_chunks")

        run_downgrade(target_url, "0067")

        assert await _column_comments(target_url, "api.raw_chunks") == columns_before
        assert await _view_comment(target_url, "api.raw_chunks") == comment_before
        assert await _view_definition(target_url, "api.raw_chunks") == definition_before
        assert await _reader_may_select(target_url, "api.raw_chunks")


class TestCutPlansMigration:
    """Revision 0069: `kb.cut_plans` holds each cut plan by knowledge base, text hash and chunk planner, granted to no reader."""

    def test_the_revision_sits_directly_on_0068(self) -> None:
        module = _load_migration_module("0069")

        assert module.revision == "0069"
        assert module.down_revision == "0068"

    async def test_the_table_is_keyed_by_collection_text_hash_and_planner_and_the_downgrade_drops_it(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0068")
        assert not await _relation_exists(target_url, "kb.cut_plans")

        run_upgrade(target_url, "0069")

        assert await _column_names(target_url, "kb.cut_plans") == {"collection", "text_hash", "planner", "starts"}
        key = await _rows(
            target_url,
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'kb.cut_plans'::regclass AND i.indisprimary ORDER BY array_position(i.indkey, a.attnum)",
        )
        assert key == [("collection",), ("text_hash",), ("planner",)]
        assert await _rows(target_url, "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'kb.cut_plans'::regclass AND attname = 'starts'") == [
            ("integer[]",)
        ]
        comment = await _view_comment(target_url, "kb.cut_plans")
        assert comment is not None
        assert "chunk planner" in comment
        assert not await _reader_may_select(target_url, "kb.cut_plans")

        run_downgrade(target_url, "0068")

        assert not await _relation_exists(target_url, "kb.cut_plans")


class TestGateModeAndRefreshResultsMigration:
    """Revision 0072: a schedule's gate behaviour lives in `gate_mode` alone, and a refresh result stored as text becomes its pass document."""

    def test_the_revision_sits_directly_on_0071(self) -> None:
        module = _load_migration_module("0072")

        assert module.revision == "0072"
        assert module.down_revision == "0071"

    async def test_the_switch_folds_into_gate_mode_and_the_downgrade_derives_it_back(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0071")
        comment_query = "SELECT col_description('public.schedules'::regclass, attnum) FROM pg_attribute WHERE attrelid = 'public.schedules'::regclass AND attname = 'gate_mode'"
        comment_before = await _rows(target_url, comment_query)
        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            await connection.executemany(
                "INSERT INTO public.schedules (id, workflow_id, cadence, gate_mode, auto_approve_gates)"
                " VALUES (gen_random_uuid(), $1, 'every 1h', $2, $3)",
                [("wf.switched", "wait", True), ("wf.auto", "auto", True), ("wf.waiting", "wait", False), ("wf.orchestrated", "orchestrate", False)],
            )
        finally:
            await connection.close()

        run_upgrade(target_url, "0072")

        assert "auto_approve_gates" not in await _column_names(target_url, "public.schedules")
        assert await _rows(target_url, "SELECT workflow_id, gate_mode FROM public.schedules ORDER BY workflow_id") == [
            ("wf.auto", "auto"),
            ("wf.orchestrated", "orchestrate"),
            ("wf.switched", "auto"),
            ("wf.waiting", "wait"),
        ], "a row whose switch said auto runs auto, and every other row keeps its mode"
        assert "auto_approve_gates" not in str(await _rows(target_url, comment_query))

        run_downgrade(target_url, "0071")

        assert await _rows(target_url, "SELECT workflow_id, gate_mode, auto_approve_gates FROM public.schedules ORDER BY workflow_id") == [
            ("wf.auto", "auto", "True"),
            ("wf.orchestrated", "orchestrate", "False"),
            ("wf.switched", "auto", "True"),
            ("wf.waiting", "wait", "False"),
        ]
        assert await _rows(target_url, comment_query) == comment_before

    async def test_a_refresh_result_stored_as_text_becomes_the_pass_document(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0071")
        document = '{"detail":"indexed 2 pages","counts":{"sources_created":2,"requested_by_entry":{},"created_by_connector":{}}}'
        connection = await asyncpg.connect(_asyncpg_dsn(target_url))
        try:
            await connection.executemany(
                "INSERT INTO coordination.jobs (kind, key, state, result) VALUES ($1, $2, $3, $4)",
                [
                    ("kb.refresh", "text", "done", "scanned 0 entries, synced 1 created, indexed 3 pages"),
                    ("kb.refresh", "document", "done", document),
                    ("kb.refresh", "queued", "queued", None),
                    ("kb.ingest", "other", "done", "left as written"),
                ],
            )
        finally:
            await connection.close()

        run_upgrade(target_url, "0072")

        rows = {str(key): result for key, result in await _rows(target_url, "SELECT key, result FROM coordination.jobs ORDER BY key")}
        assert json.loads(str(rows["text"])) == {
            "detail": "scanned 0 entries, synced 1 created, indexed 3 pages",
            "counts": {"sources_created": 0, "requested_by_entry": {}, "created_by_connector": {}},
        }
        assert rows["document"] == document
        assert rows["queued"] is None
        assert rows["other"] == "left as written"


    async def test_the_indexed_schema_function_is_dropped_and_the_downgrade_restores_it(self, pg: str):
        target_url = await new_test_database(pg)
        run_upgrade(target_url, "0071")
        definition_before = await _function_definition(target_url, _INDEXED_SCHEMA)
        comment_before = await _function_comment(target_url, schema="api", name="indexed_schema")
        column_comment_before = await _rows(target_url, _INDEXED_SCHEMA_COLUMN_COMMENT)
        assert definition_before is not None
        assert await _reader_may_execute(target_url, _INDEXED_SCHEMA)

        run_upgrade(target_url, "0072")

        assert not await _function_exists(target_url, _INDEXED_SCHEMA)
        assert "indexed_schema" not in str(await _rows(target_url, _INDEXED_SCHEMA_COLUMN_COMMENT))

        run_downgrade(target_url, "0071")

        assert await _function_definition(target_url, _INDEXED_SCHEMA) == definition_before
        assert await _function_comment(target_url, schema="api", name="indexed_schema") == comment_before
        assert await _reader_may_execute(target_url, _INDEXED_SCHEMA)
        assert await _rows(target_url, _INDEXED_SCHEMA_COLUMN_COMMENT) == column_comment_before


_INDEXED_SCHEMA = "api.indexed_schema(text)"

_INDEXED_SCHEMA_COLUMN_COMMENT = (
    "SELECT col_description('kb.index_fingerprint'::regclass, attnum) FROM pg_attribute"
    " WHERE attrelid = 'kb.index_fingerprint'::regclass AND attname = 'spec'"
)


async def _function_definition(url: str, signature: str) -> str | None:
    """The full `CREATE FUNCTION` text of exactly this signature, or None when the database carries none."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return await connection.fetchval("SELECT pg_get_functiondef(to_regprocedure($1))", signature)
    finally:
        await connection.close()
