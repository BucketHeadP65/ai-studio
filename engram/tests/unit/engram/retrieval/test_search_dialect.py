r"""Unit specs for the BM25 dialect seam registry (`engram.retrieval.search_dialect`).

No database and no network: the vocabulary is pure plumbing — a frozen
:class:`SearchDialects` dataclass wrapping ``Mapping[DbDialect, SearchDialect]``
that dispatches by enum member (mirrors `CalendarAdapters` /
`OAuthProviderRegistry` in the server's calendar slice).

A hand-written :class:`FakeDialect` structurally implements the
:class:`SearchDialect` Protocol so it is accepted by the vocabulary **without**
`cast(...)` — proving the port is structural (the real ParadeDB / pg_textsearch
dialects in tasks 2.2 / 2.3 need no inheritance either). The fake records its
calls so callers can assert the keyword contract of `bm25_index_ddl`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypedDict

import pytest

from engram.retrieval.search_dialect import DbDialect, SearchDialects
from engram.retrieval.search_dialect.pg_search import PgSearchDialect
from engram.retrieval.search_dialect.pg_textsearch import PgTextSearchDialect

if TYPE_CHECKING:
    from engram.retrieval.search_dialect.dialect import SearchDialect


class IndexDdlCall(TypedDict):
    """One recorded :meth:`FakeDialect.bm25_index_ddl` call — its keyword arguments."""

    index: str
    table: str
    key: str
    text_cols: tuple[str, ...]


class FakeDialect:
    """Structural stand-in for a real `SearchDialect`.

    `name` is a public attribute and the two SQL emitters return canned
    strings so the vocabulary can be exercised without a database.
    """

    def __init__(self, name: Literal["pg_search", "pg_textsearch"]) -> None:
        self.name: Literal["pg_search", "pg_textsearch"] = name
        self.index_ddl_calls: list[IndexDdlCall] = []

    def create_bm25_extension_ddl(self) -> str:
        return f"-- create bm25 extension for {self.name}"

    def bm25_index_ddl(self, *, index: str, table: str, key: str, text_cols: tuple[str, ...]) -> str:
        self.index_ddl_calls.append({"index": index, "table": table, "key": key, "text_cols": text_cols})
        return f"CREATE INDEX {index} ON {table} USING bm25 ({key})"

    def api_search_body(self) -> str:
        return f"-- api.search body for {self.name}"


class TestDbDialect:
    def test_has_exactly_pg_search_and_pg_textsearch(self):
        assert {member.value for member in DbDialect} == {"pg_search", "pg_textsearch"}

    def test_is_a_strenum(self):
        assert issubclass(DbDialect, StrEnum)
        assert DbDialect.PG_SEARCH == "pg_search"
        assert DbDialect.PG_TEXTSEARCH == "pg_textsearch"


class TestSearchDialects:
    def test_select_returns_the_registered_strategy_per_enum_member(self):
        pg_search, pg_textsearch = FakeDialect("pg_search"), FakeDialect("pg_textsearch")
        vocabulary = SearchDialects(by_key={DbDialect.PG_SEARCH: pg_search, DbDialect.PG_TEXTSEARCH: pg_textsearch})

        assert vocabulary.select(DbDialect.PG_SEARCH) is pg_search
        assert vocabulary.select(DbDialect.PG_TEXTSEARCH) is pg_textsearch

    def test_accepts_a_structural_fake_without_cast(self):
        # The fake satisfies the Protocol by shape alone.
        fake: SearchDialect = FakeDialect("pg_search")
        vocabulary = SearchDialects(by_key={DbDialect.PG_SEARCH: fake})

        selected = vocabulary.select(DbDialect.PG_SEARCH)

        assert selected.name == "pg_search"
        ddl = selected.bm25_index_ddl(index="pages_bm25", table="kb.pages", key="id", text_cols=("title", "body"))
        assert "USING bm25" in ddl
        assert selected.api_search_body() == "-- api.search body for pg_search"

    def test_bm25_index_ddl_forwards_the_keyword_contract(self):
        fake = FakeDialect("pg_search")
        vocabulary = SearchDialects(by_key={DbDialect.PG_SEARCH: fake})

        vocabulary.select(DbDialect.PG_SEARCH).bm25_index_ddl(index="pages_bm25", table="kb.pages", key="id", text_cols=("title", "body"))

        assert fake.index_ddl_calls == [{"index": "pages_bm25", "table": "kb.pages", "key": "id", "text_cols": ("title", "body")}]

    def test_select_propagates_keyerror_for_an_unregistered_dialect(self):
        vocabulary = SearchDialects(by_key={DbDialect.PG_SEARCH: FakeDialect("pg_search")})

        with pytest.raises(KeyError):
            vocabulary.select(DbDialect.PG_TEXTSEARCH)


class TestPgSearchDialect:
    """The safety-net dialect must emit today's ParadeDB BM25 grammar verbatim."""

    def test_satisfies_searchdialect_structurally_without_cast(self):
        # The concrete dialect binds into the vocabulary by shape alone.
        dialect: SearchDialect = PgSearchDialect()
        vocabulary = SearchDialects(by_key={DbDialect.PG_SEARCH: dialect})

        assert vocabulary.select(DbDialect.PG_SEARCH) is dialect

    def test_name_is_the_paradedb_literal(self):
        assert PgSearchDialect().name == "pg_search"

    def test_create_bm25_extension_ddl_targets_pg_search_verbatim(self):
        # The safety net: ParadeDB's extension statement is 0001's CREATE EXTENSION ... pg_search,
        # preserved verbatim (IF NOT EXISTS), never pg_textsearch.
        ddl = PgSearchDialect().create_bm25_extension_ddl()

        assert ddl == "CREATE EXTENSION IF NOT EXISTS pg_search"
        assert "pg_textsearch" not in ddl

    def test_bm25_index_ddl_emits_todays_paradedb_shape(self):
        ddl = PgSearchDialect().bm25_index_ddl(
            index="pages_bm25", table="kb.pages", key="id", text_cols=("title", "summary", "card_text")
        )

        assert "CREATE INDEX pages_bm25 ON kb.pages USING bm25" in ddl
        assert "key_field='id'" in ddl

    def test_bm25_index_ddl_leads_the_column_list_with_the_key_then_text_cols(self):
        ddl = PgSearchDialect().bm25_index_ddl(
            index="sections_bm25", table="kb.sections", key="id", text_cols=("heading", "canon", "body", "page_id")
        )

        assert "(id, heading, canon, body, page_id)" in ddl

    def test_api_search_body_carries_the_paradedb_bm25_grammar(self):
        body = PgSearchDialect().api_search_body()

        assert "CREATE FUNCTION api.search(" in body
        assert "@@@" in body
        assert "paradedb.parse" in body
        assert "paradedb.score" in body
        assert "MATERIALIZED" in body

    def test_api_search_body_keeps_the_rrf_merge_and_topk_cap(self):
        body = PgSearchDialect().api_search_body()

        assert "sum(legs.w / ($" in body  # each leg weighed over the tuned constant, bound by position
        assert "least(greatest(k, 1), least(greatest((tuning ->> 'k_ceiling')::numeric::int, 1), 50))" in body

    def test_api_search_body_excludes_the_pg_textsearch_grammar(self):
        # Pin the safety net: to_bm25query / <@> belong to the pg_textsearch
        # (`pg_textsearch`) dialect, never to ParadeDB.
        body = PgSearchDialect().api_search_body()

        assert "to_bm25query" not in body
        assert "<@>" not in body


class TestPgTextSearchDialect:
    """The `pg_textsearch` dialect rewrites the BM25 legs to `pg_textsearch` grammar.

    The function contract (signature, RETURNS columns, `SECURITY DEFINER`, the
    vector legs, the RRF fusion, the top-k cap) is identical to ParadeDB; only the
    four BM25 legs swap `@@@` / `paradedb.parse` / `paradedb.score` for
    `<@>` / `to_bm25query` and the two ParadeDB PL/pgSQL workarounds drop.
    """

    def test_satisfies_searchdialect_structurally_without_cast(self):
        # Binds into the vocabulary by shape alone.
        dialect: SearchDialect = PgTextSearchDialect()
        vocabulary = SearchDialects(by_key={DbDialect.PG_TEXTSEARCH: dialect})

        assert vocabulary.select(DbDialect.PG_TEXTSEARCH) is dialect

    def test_name_is_the_pg_textsearch_literal(self):
        assert PgTextSearchDialect().name == "pg_textsearch"

    def test_create_bm25_extension_ddl_targets_pg_textsearch(self):
        # `pg_textsearch` swaps the BM25 extension to pg_textsearch — same statement shape as ParadeDB's,
        # never pg_search.
        ddl = PgTextSearchDialect().create_bm25_extension_ddl()

        assert ddl == "CREATE EXTENSION IF NOT EXISTS pg_textsearch"
        assert "pg_search" not in ddl

    def test_bm25_index_ddl_emits_the_pg_textsearch_shape(self):
        # `table` is deliberately `wiki.pages`, not `kb.pages`: `_BM25_SEARCH_COLUMN` stays keyed
        # on the pre-de-Obsidianification table names forever — its only caller is the frozen
        # migration 0001, which always passes `wiki.pages` literally (see the module docstring).
        ddl = PgTextSearchDialect().bm25_index_ddl(
            index="pages_bm25", table="wiki.pages", key="id", text_cols=("title", "summary", "card_text")
        )

        assert "CREATE INDEX pages_bm25 ON wiki.pages USING bm25" in ddl
        assert "text_config='english'" in ddl

    def test_bm25_index_ddl_emits_a_single_column_without_the_key_or_key_field(self):
        # pg_textsearch's bm25 access method is single-column (a multi-column bm25 index is
        # rejected by the engine). The one column is
        # the surface's <@> search column (sections → body), never the key and never key_field.
        ddl = PgTextSearchDialect().bm25_index_ddl(
            index="sections_bm25", table="wiki.sections", key="id", text_cols=("heading", "canon", "body")
        )

        assert "USING bm25 (body)" in ddl
        assert "key_field" not in ddl
        assert "(id," not in ddl
        # The unused ParadeDB filter columns are not indexed — a multi-column index would fail.
        assert "heading" not in ddl
        assert "canon" not in ddl

    def test_bm25_index_ddl_indexes_each_surfaces_single_search_column(self):
        # Each BM25 index covers exactly the one column its <@> leg in api.search matches,
        # keeping the index and the to_bm25query operand in lockstep with the surface's text_col
        # in KB_SEARCH_SCHEMA (the builder generates the <@> operand from it).
        dialect = PgTextSearchDialect()

        for index, table, search_col in (
            ("pages_bm25", "wiki.pages", "card_text"),
            ("sections_bm25", "wiki.sections", "body"),
            ("claims_bm25", "wiki.claims", "text"),
            ("raw_chunks_bm25", "wiki.raw_chunks", "body"),
        ):
            ddl = dialect.bm25_index_ddl(index=index, table=table, key="id", text_cols=("ignored",))

            assert ddl == f"CREATE INDEX {index} ON {table} USING bm25 ({search_col}) WITH (text_config='english')"

    def test_api_search_body_keeps_the_function_contract(self):
        body = PgTextSearchDialect().api_search_body()

        assert "api.search(" in body
        assert (
            "RETURNS TABLE (id text, src_collection text, hit_level text, page_id text, page_title text, snippet text, "
            "score float8, lexical_rank int, vector_rank int, vector_distance float8, snippet_truncated boolean)"
        ) in body
        assert "SECURITY DEFINER" in body

    def test_api_search_body_carries_the_pg_textsearch_bm25_grammar(self):
        body = PgTextSearchDialect().api_search_body()

        assert "to_bm25query" in body
        assert "<@>" in body

    def test_api_search_body_names_each_bm25_index_in_its_leg(self):
        # Each of the four BM25 legs must name its own index in to_bm25query so the
        # operator resolves against the right relation's index (matches 2.5's DDL).
        body = PgTextSearchDialect().api_search_body()

        assert "'pages_bm25'" in body
        assert "'sections_bm25'" in body
        assert "'claims_bm25'" in body
        assert "'raw_chunks_bm25'" in body

    def test_api_search_body_excludes_the_paradedb_grammar(self):
        body = PgTextSearchDialect().api_search_body()

        assert "paradedb." not in body
        assert "@@@" not in body
        assert "paradedb.score" not in body
        assert "paradedb.parse" not in body

    def test_api_search_body_keeps_the_vector_legs(self):
        # The four vector legs keep the <=> cosine-distance operator and rank
        # logic. The static body references query_vec by name (the dynamic
        # EXECUTE that bound $3 positionally is dropped along with the workaround).
        body = PgTextSearchDialect().api_search_body()

        assert "<=>" in body
        assert "embedding <=> query_vec" in body

    def test_api_search_body_keeps_the_rrf_merge_and_topk_cap(self):
        body = PgTextSearchDialect().api_search_body()

        assert "sum(legs.w / (rrf_constant + legs.r))" in body
        assert "least(greatest(k, 1), least(greatest((tuning ->> 'k_ceiling')::numeric::int, 1), 50))" in body

    def test_api_search_body_resolves_the_collection_variable_conflict(self):
        # The static body references params by name, so the `collection` param collides with the
        # `collection` table column. `#variable_conflict use_variable` resolves bare param names to
        # the parameter (table columns stay qualified) — without it the call raises AmbiguousColumnError.
        body = PgTextSearchDialect().api_search_body()

        assert "#variable_conflict use_variable" in body

    def test_api_search_body_drops_the_paradedb_plpgsql_workarounds(self):
        # The simplified static form: <@> is an ordinary operator expression, so
        # neither the MATERIALIZED-per-leg CTEs nor the dynamic RETURN QUERY EXECUTE
        # are needed. Pin their absence so a regression to the workaround shape fails.
        body = PgTextSearchDialect().api_search_body()

        assert "MATERIALIZED" not in body
        assert "EXECUTE" not in body

    def test_api_search_body_bounds_each_bm25_leg_with_the_topk_pattern(self):
        # The spec's documented top-k pattern: each BM25 leg self-bounds with
        # ORDER BY col <@> to_bm25query(...) LIMIT leg_budget (the row_number()
        # window is evaluated before LIMIT, so ranks survive). The body is now
        # generated by the library builder, so pin the terminal ordering+limit
        # fragment structurally (whitespace-collapsed), not by exact indentation.
        collapsed = " ".join(PgTextSearchDialect().api_search_body().split())

        for column, index in (
            ("p.card_text", "pages_bm25"),
            ("s.body", "sections_bm25"),
            ("c.text", "claims_bm25"),
            ("r.body", "raw_chunks_bm25"),
        ):
            assert f"ORDER BY {column} <@> to_bm25query(query, '{index}') LIMIT leg_budget" in collapsed

    def test_api_search_body_filters_each_bm25_leg_to_actual_matches(self):
        # Regression guard (zero-hits bug): pg_textsearch ships only the rank operator <@>, which
        # scores EVERY row (non-matches score 0.0, matches < 0) rather than filtering like ParadeDB's
        # boolean match operator. Each BM25 leg must therefore carry `WHERE ... col <@> to_bm25query
        # (query, 'idx') < 0` as the boolean match filter — without it a non-matching query returns the
        # whole capped pool instead of []. Assert the exact match predicate for each of the four legs.
        body = PgTextSearchDialect().api_search_body()

        for column, index in (
            ("p.card_text", "pages_bm25"),
            ("s.body", "sections_bm25"),
            ("c.text", "claims_bm25"),
            ("r.body", "raw_chunks_bm25"),
        ):
            assert f"{column} <@> to_bm25query(query, '{index}') < 0" in body

    def test_api_search_body_qualifies_the_page_pool_semijoin_id(self):
        # Regression guard (zero-hits bug): under `#variable_conflict use_variable` a bare `id` binds
        # to the NULL RETURNS-TABLE OUT variable `id`, not a column — so `SELECT id FROM page_pool`
        # silently became `SELECT NULL`, emptying every leg's pool. The semi-join must select the
        # relation-qualified `page_pool.id`, never a bare `id`.
        body = PgTextSearchDialect().api_search_body()

        assert "(p.collection, p.id) IN (SELECT page_pool.collection, page_pool.id FROM page_pool)" in body
        assert "(SELECT id FROM page_pool)" not in body

    def test_api_search_body_qualifies_each_legs_union_projection(self):
        # The `legs` UNION ALL projects each leg's columns; a bare `id`/`page_id`/`snippet`/… would
        # again bind to the OUT variables under `use_variable` and collapse the union. Every projected
        # column must be relation-qualified by its source CTE (spot-check the id column per leg).
        body = PgTextSearchDialect().api_search_body()

        for cte in ("card_bm25", "card_vec", "section_bm25", "section_vec", "claim_bm25", "claim_vec", "raw_bm25", "raw_vec"):
            assert f"{cte}.id, {cte}.partition_key, {cte}.lvl, {cte}.page_id, {cte}.page_title, {cte}.snippet" in body

    def test_api_search_body_caps_the_fused_result_set(self):
        # The result keeps the k cap after the policies: a lone exact title first, then the fused order.
        # Whitespace-collapsed: the generated body's indentation is not the contract.
        assert "ORDER BY ruled.pin_alone DESC, ruled.rrf DESC, ruled.id LIMIT capped_k;" in " ".join(PgTextSearchDialect().api_search_body().split())
