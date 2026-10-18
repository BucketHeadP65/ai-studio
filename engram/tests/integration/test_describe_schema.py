r"""The api M-Schema introspection, against a real migrated ParadeDB — Docker-gated.

``DescribeSchemaHandler`` (over :class:`~engram.retrieval.infrastructure.gateway.ReaderSqlGateway`) renders
what the `describe_schema` tool serves to agents: a compact catalog of every `api` object (the
~14 views + 4 functions of migration 0002) with no `objects` named, or the full COMMENT + typed
columns for up to `SCHEMA_OBJECT_LIMIT` named ones. This tier proves the introspection SQL
(`pg_class` / `pg_proc` / `obj_description` / `col_description`, `names`-filtered or not)
actually surfaces every object and its comments off the *as-built* schema — the unit specs in
`tests/unit/engram/retrieval/test_queries.py` only exercise the rendering over a hand-built
`SchemaDescription`.

Reads go through the read-only `kb_reader` role (the `reader_url` fixture): the M-Schema
lives in the `api` schema the role is granted, so no admin access is needed. The whole module
skips when Docker is unavailable (the `pg` fixture, via `migrated`, raises `pytest.skip`).

The tests are *synchronous*: each wraps its gateway/handler reads in `asyncio.run` (legal —
the sync test body has no running loop), matching `tests/e2e/test_compile_and_search.py`.
"""

from __future__ import annotations

import asyncio
import re

from typing import TYPE_CHECKING

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine

from engram.indexing.infrastructure import orm
from engram.retrieval.domain import DescribeSchema, SchemaDescription
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.queries import SCHEMA_RESULT_CHARS, DescribeSchemaHandler
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from engram.retrieval.domain import ObjectInfo


def _run_sync[T](coro: Coroutine[object, object, T]) -> T:
    """Drive a coroutine to completion from a synchronous test body (no loop is running)."""
    return asyncio.run(coro)


# The views and functions migration 0002 creates in the `api` schema (the agent-facing surface) that a
# database at head still carries. `describe_schema`'s catalog must list all of them.
_EXPECTED_VIEWS: tuple[str, ...] = (
    "api.about",
    "api.schema_guide",
    "api.cards",
    "api.sections",
    "api.claims",
    "api.edges",
    "api.contradictions",
    "api.raw_chunks",
    "api.stale",
)
_EXPECTED_FUNCTIONS: tuple[str, ...] = ("api.search", "api.fetch", "api.lookup_value", "api.log_session")


def _api_objects(reader_url: str, names: tuple[str, ...] = ()) -> SchemaDescription:
    """Introspect the `api` surface through the reader gateway (one `asyncio.run`), `names`-filtered or not."""

    async def _go() -> SchemaDescription:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            return await ReadOnlySqlGateway(engine).api_objects(names=names)
        finally:
            await engine.dispose()

    return _run_sync(_go())


def _catalog(reader_url: str) -> str:
    """The compact catalog `describe_schema()` (no `objects` named) serves, under the bundled research spec."""

    async def _go() -> str:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            handler = DescribeSchemaHandler(ReadOnlySqlGateway(engine), (research_spec(),))
            return await handler(DescribeSchema())
        finally:
            await engine.dispose()

    return _run_sync(_go())


def _detail(reader_url: str, objects: tuple[str, ...]) -> str:
    """The full column detail `describe_schema(objects=...)` serves for `objects`, under the bundled research spec."""

    async def _go() -> str:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            handler = DescribeSchemaHandler(ReadOnlySqlGateway(engine), (research_spec(),))
            return await handler(DescribeSchema(objects=objects))
        finally:
            await engine.dispose()

    return _run_sync(_go())


def _by_name(description: SchemaDescription) -> dict[str, ObjectInfo]:
    return {obj.name: obj for obj in description.objects}


class TestApiObjects:
    def test_every_view_and_function_is_described(self, reader_url: str):
        names = {obj.name for obj in _api_objects(reader_url).objects}

        assert set(_EXPECTED_VIEWS) <= names
        assert set(_EXPECTED_FUNCTIONS) <= names

    def test_views_carry_kind_view_and_functions_kind_function(self, reader_url: str):
        by_name = _by_name(_api_objects(reader_url))

        assert by_name["api.cards"].kind == "view"
        assert by_name["api.search"].kind == "function"

    def test_object_comments_surface_for_at_least_five_objects(self, reader_url: str):
        # The COMMENTs ARE the M-Schema; spot-check a representative spread of objects.
        by_name = _by_name(_api_objects(reader_url))
        commented = {name: by_name[name].comment for name in ("api.about", "api.cards", "api.claims", "api.search", "api.fetch")}

        assert all(comment for comment in commented.values())
        assert "card scan" in (by_name["api.cards"].comment or "")

    def test_search_comment_documents_its_params(self, reader_url: str):
        # The search function's COMMENT is where agents learn the vector_query shim / k-cap semantics.
        comment = _by_name(_api_objects(reader_url))["api.search"].comment or ""

        assert "vector_query" in comment
        assert "k caps how many hits come back (default 12, at most the owner's k ceiling, 50 unless the owner sets it lower)" in comment

    def test_columns_have_types_and_comments(self, reader_url: str):
        cards = _by_name(_api_objects(reader_url))["api.cards"]
        columns = {c.name: c for c in cards.columns}

        assert columns["id"].data_type == "text"
        assert columns["id"].comment
        assert columns["type"].comment  # the renamed page_type column carries its own COMMENT

    def test_names_filters_the_introspection_to_just_those_objects(self, reader_url: str):
        description = _api_objects(reader_url, names=("cards", "search"))

        assert {obj.name for obj in description.objects} == {"api.cards", "api.search"}


class TestRenderedSchema:
    def test_the_catalog_lists_every_api_object_by_name(self, reader_url: str):
        rendered = _catalog(reader_url)

        for name in (*_EXPECTED_VIEWS, *_EXPECTED_FUNCTIONS):
            assert name in rendered

    def test_the_catalog_stays_within_the_size_budget(self, reader_url: str):
        assert len(_catalog(reader_url)) <= SCHEMA_RESULT_CHARS

    def test_named_detail_carries_object_and_column_comments(self, reader_url: str):
        rendered = _detail(reader_url, ("api.cards", "api.search"))

        assert "L1 card scan" in rendered  # api.cards object comment
        assert "Page id" in rendered  # api.cards.id column comment
        assert "query_vec" in rendered  # api.search function comment

    def test_a_full_batch_of_named_detail_stays_within_the_size_budget(self, reader_url: str):
        assert len(_detail(reader_url, _EXPECTED_VIEWS[:8])) <= SCHEMA_RESULT_CHARS


_RESEARCH_WORDS = ("paper.", "ent.", "lightrag", "graphrag", "hotpotqa", "'research'", "playbook", "long-doc-qa", "benchmark-result", "tasks", "technique")


def _every_api_comment(reader_url: str) -> dict[str, str]:
    """Every comment on an `api` relation, column or function, keyed by name, as the gateway's describe queries read them."""
    comments: dict[str, str] = {}
    for obj in _api_objects(reader_url).objects:
        key = obj.name
        while key in comments:
            key += "'"
        comments[key] = obj.comment or ""
        for column in obj.columns:
            comments[f"{obj.name}.{column.name}"] = column.comment or ""
    return comments


class TestCommentsNameNoKnowledgeBase:
    def test_no_api_comment_carries_a_research_word(self, reader_url: str):
        comments = _every_api_comment(reader_url)

        offending = {name: word for name, text in comments.items() for word in _RESEARCH_WORDS if re.search(rf"(?<![a-z]){re.escape(word)}", text.lower())}
        assert offending == {}
