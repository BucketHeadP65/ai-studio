r"""Unit specs for the serve-slice MCP server factory (`src/engram/retrieval/infrastructure/mcp_server.py`).

These pin the MCPServer wiring **without** a transport or a database: the factory is
DI-friendly (its tools close over the injected gateway + describe handler — no
container/env reads inside), so a unit test can build a server from a
:class:`FakeSqlGateway` + a real :class:`DescribeSchemaHandler` over a fake, then
drive the tools through the SDK's in-memory client↔server session
(:func:`mcp.shared.memory.create_connected_server_and_client_session`, which accepts a
:class:`~mcp.server.mcpserver.MCPServer` directly). They fix the binding contract:

* the server registers **exactly two** tools — `describe_schema` and `run_sql` —
  with non-empty descriptions that orient the agent (the query surfaces and a cheapest-first
  order across them, the optional `vector_query` of `api.search`, what the guard refuses).
* `describe_schema()` returns the M-Schema text the injected handler renders;
* `run_sql(sql, row_limit)` calls the gateway with `cap = min(row_limit, 50)` (the
  hard cap), defaulting `row_limit` to 50, and returns `{"rows": [...], "note": ...}`
  as `structuredContent`;
* the gateway's truncation note (`"… N more rows — narrow your filters"`) rides in `note`;
* a read-only / DB error from the role **fails the call** with an actionable message (the
  read-only hint), never an uncaught crash and never a success.
"""

from __future__ import annotations

import json

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

import pytest

from core.ai.retrieval.contracts import KnowledgeBaseStamp
from core.ai.retrieval.strategy import resolve_strategy
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from sqlalchemy.exc import DBAPIError

from engram.authoring.contracts import AuthoringKnowledgeBase, AuthoringTools
from engram.indexing.contracts import RetrievalSwitches
from engram.retrieval.capability import ReadSurface
from engram.retrieval.domain import ColumnInfo, GraphPathsResult, GraphResult, ObjectInfo, SchemaDescription, SearchResult, SqlResult
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.infrastructure.embedded import slug_only_stamp
from engram.retrieval.infrastructure.mcp_server import _build_sandbox_toolset, create_mcp_server
from engram.retrieval.queries import (
    _GRAPH_ROW_CAP,
    GRAPH_RESULT_CHARS,
    DescribeSchemaHandler,
    FetchHandler,
    GraphExpandHandler,
    GraphPathsHandler,
    SearchHandler,
)
from engram.retrieval.sql_tools import make_describe_schema
from engram.retrieval.tools import FetchEnvelope, FetchUnit, RetrievalHandlers, register_routed_mcp_tools
from engram.sources.contracts import SourceQueue, claimants_for
from engram.testing.fakes.authoring import FakeAdminGateway, FakeReindex
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.ai.retrieval.contracts import StampSource
    from mcp.types import CallToolResult


_COLLECTION = "workdocs"
"""The made-up collection every door in this module is built over: there is no default one."""

_FLAG_PAGE = """---
id: flag.try-planning-reminders
type: flag
title: Try planning reminders
summary: A live task list re-injected every turn.
status: pending
flag_kind: idea
about: ["[[talk.agents-that-plan]]"]
---

## Why

Because compaction drops the plan.

## Proposal

Mount the planning capability.

## Evidence

talk.agents-that-plan#findings
"""

_ABOUT_RESULT = SqlResult(rows=({"body": "Welcome to the memory."},), columns=("body",))


def _schema() -> SchemaDescription:
    """A one-object M-Schema the describe handler renders (api.cards + api.search names)."""
    return SchemaDescription(
        objects=(
            ObjectInfo(
                name="api.cards",
                kind="view",
                comment="L1 card scan — one row per page.",
                columns=(ColumnInfo(name="id", data_type="text", comment="Page id."),),
            ),
        )
    )


def _describe_handler(schema: SchemaDescription) -> DescribeSchemaHandler:
    """The real (pure) describe handler over a fake gateway scripted with `schema`."""
    return DescribeSchemaHandler(FakeSqlGateway(schema=schema))


def _text(result: CallToolResult) -> str:
    """The first text block of a tool result (the tools return a single text payload)."""
    block = result.content[0]
    assert block.type == "text"
    return block.text


def _node_cells(
    node_id: str, *, type_: str = "entity", title: str = "T", status: str = "verified", superseded: bool = False
) -> dict[str, object]:
    """The node-shaped columns `api.graph_expand` / `api.graph_paths` return per row."""
    return {"id": node_id, "type": type_, "title": title, "status": status, "superseded": superseded}


def _edge_cells(src_id: str | None, dst_id: str | None, rel: str | None, provenance: str | None = None) -> dict[str, object]:
    """The edge-shaped columns of a graph row; a row reaching no node leaves `rel` NULL."""
    return {"src_id": src_id, "dst_id": dst_id, "rel": rel, "provenance_claim": provenance}


def _no_reach(gateway: FakeSqlGateway) -> FakeSqlGateway:
    """`gateway`, answering `api.graph_reach` with no row unless the test routed it."""
    gateway.routed.setdefault("api.graph_reach", SqlResult(rows=(), columns=()))
    return gateway


def _graph_server(gateway: FakeSqlGateway) -> MCPServer:
    """A server wired with both graph tools' handlers plus `fetch`, but no `search`.

    The `graph-only` strategy's own shape (`resolve_strategy("graph-only").tools`): a traversal an
    agent can also read, with no search tool to ground an id from.
    """
    return create_mcp_server(
        FakeSqlGateway(),
        _describe_handler(_schema()),
        fetch_handler=FetchHandler(gateway, "research"),
        graph_expand_handler=GraphExpandHandler(_no_reach(gateway), "research"),
        find_paths_handler=GraphPathsHandler(gateway, "research"),
        collection=_COLLECTION,
    )


def _embed_query(embedder: FakeEmbedding):
    """An embed-one-text callable backed by `embedder` — mirrors the CLI's lazy thunk."""

    async def embed(text: str) -> list[float]:
        embeddings = await embedder.embed_batch([text])
        return embeddings.vectors[0]

    return embed


def _full_server(
    gateway: FakeSqlGateway,
    embedder: FakeEmbedding | None = None,
    *,
    only: frozenset[str] | None = None,
    descriptions: dict[str, str] | None = None,
    k_source: Callable[[], int] | None = None,
    envelope_handler: EnvelopeHandler | None = None,
    stamp: StampSource | None = None,
    switches: RetrievalSwitches | None = None,
) -> MCPServer:
    """A server wired with the whole retrieval surface: search/fetch (single source) + graph.

    The single-source `search` / `fetch` tools (`engram.retrieval.tools`) are exposed only when their
    handlers are wired (the full-retrieval deployment), alongside the rich `graph_expand` /
    `find_paths` — every retrieval handler reads through the same `gateway` so a tool call's
    SQL + params are recordable on it. `only` gates the tool surface (it flows the `code-mode`
    profile's set through so the `run_code` tests can opt into the sandbox). `descriptions`
    is the tuning override map threaded onto the four retrieval tools (C9b). `k_source` hands
    the search row cap to the host, which takes `k` out of the served tool. `envelope_handler`,
    when given, turns both the top-level `fetch` AND the `run_code` sandbox's `fetch` into the
    leveled read — `None` (the default) keeps every retrieval tool flat, as every test above this
    parameter's addition already assumes. `stamp`, when given, rides every search and fetch answer
    as `knowledge_base`. `None` (the default) leaves it unset, as every test above this parameter's
    addition already assumes.
    """
    return create_mcp_server(
        gateway,
        _describe_handler(_schema()),
        _embed_query(embedder) if embedder is not None else None,
        search_handler=SearchHandler(gateway, embedder or FakeEmbedding()),
        fetch_handler=FetchHandler(gateway, "research"),
        graph_expand_handler=GraphExpandHandler(_no_reach(gateway), "research"),
        find_paths_handler=GraphPathsHandler(gateway, "research"),
        envelope_handler=envelope_handler,
        only=only,
        descriptions=descriptions,
        k_source=k_source,
        stamp=stamp,
        switches=switches,
        collection=_COLLECTION,
    )


class TestToolRegistration:
    async def test_registers_exactly_describe_schema_and_run_sql(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()

        assert {tool.name for tool in listing.tools} == {"describe_schema", "run_sql"}

    async def test_tool_descriptions_orient_the_agent(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        assert all(by_name.values())  # both descriptions are non-empty
        blob = " ".join(by_name.values())
        assert "api.cards" in blob  # names a concrete entry surface to scan first
        assert "how to query" in blob.lower()  # framed as query mechanics, not the knowledge base's subject
        assert "api.search" in blob  # the hybrid-search entry
        assert "vector_query" in blob  # the text arg the server embeds for you (→ hybrid)
        assert "WITH RECURSIVE" in blob  # says what the guard refuses


class _RecordingFeedbackSink:
    """A :class:`~engram.retrieval.domain.FeedbackSink` recording each call's ``(category, message, tool)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def __call__(self, *, category: str, message: str, tool: str | None) -> None:
        self.calls.append((category, message, tool))


class TestFeedback:
    async def test_submit_feedback_is_absent_without_a_sink(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()

        assert "submit_feedback" not in {tool.name for tool in listing.tools}

    async def test_submit_feedback_is_registered_when_a_sink_is_wired(self):
        server = create_mcp_server(
            FakeSqlGateway(), _describe_handler(_schema()), feedback_sink=_RecordingFeedbackSink(), collection=_COLLECTION
        )

        async with Client(server) as client:
            listing = await client.list_tools()

        assert "submit_feedback" in {tool.name for tool in listing.tools}

    async def test_submit_feedback_records_category_message_and_tool_via_the_sink(self):
        sink = _RecordingFeedbackSink()
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), feedback_sink=sink, collection=_COLLECTION)

        async with Client(server) as client:
            await client.call_tool(
                "submit_feedback", {"message": "the search description is unclear", "category": "usage", "tool": "search"}
            )

        assert sink.calls == [("usage", "the search description is unclear", "search")]


class TestDescribeSchemaTool:
    async def test_no_arguments_returns_the_compact_catalog(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            result = await client.call_tool("describe_schema", {})

        assert result.is_error is False
        text = _text(result)
        assert "api.cards" in text
        assert "L1 card scan" in text
        assert "Page id." not in text  # column detail is a named-object ask, not the catalog's

    async def test_named_objects_return_the_full_column_detail(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            result = await client.call_tool("describe_schema", {"objects": ["api.cards"]})

        assert result.is_error is False
        text = _text(result)
        assert "api.cards" in text
        assert "Page id." in text

    async def test_the_objects_argument_is_optional_in_the_published_schema(self):
        """Start with no arguments, then request named objects — `objects` must never be required."""
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            listing = {tool.name: tool for tool in (await client.list_tools()).tools}

        schema = listing["describe_schema"].input_schema
        assert "objects" in (schema.get("properties") or {})  # the argument exists …
        assert "objects" not in (schema.get("required") or [])  # … and is never required

    async def test_too_many_named_objects_fails_the_call(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            result = await client.call_tool("describe_schema", {"objects": [f"api.obj{i}" for i in range(9)]})

        assert result.is_error


class TestRunSqlTool:
    async def test_caps_row_limit_at_fifty(self):
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)
        server = create_mcp_server(gateway, _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            await client.call_tool("run_sql", {"sql": "SELECT * FROM api.about", "row_limit": 999})

        assert gateway.last.cap == 50  # the hard cap
        assert gateway.last.sql == "SELECT * FROM (SELECT * FROM api.about) AS q LIMIT 51"

    async def test_default_row_limit_is_fifty(self):
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)
        server = create_mcp_server(gateway, _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            await client.call_tool("run_sql", {"sql": "SELECT * FROM api.about"})

        assert gateway.last.cap == 50

    async def test_honours_a_smaller_row_limit(self):
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)
        server = create_mcp_server(gateway, _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            await client.call_tool("run_sql", {"sql": "SELECT * FROM api.cards", "row_limit": 5})

        assert gateway.last.cap == 5

    async def test_returns_the_rows_as_structured_content(self):
        """`{"rows": [{column: value}], "note": ...}`, delivered as `structuredContent`.

        The assertion is on `structuredContent`, not on the text block: `_text(result)` is satisfied
        by a JSON dump *and* by a rendered pipe table, so it pins no shape. This one cannot pass
        against a rendered table.

        An OBJECT rather than a bare list of rows, because MCP requires `structuredContent` to be an
        object: a `-> list` return is wrapped by MCPServer as `{"result": [...]}`, which publishes a
        schema disagreeing with what clients deliver.
        """
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)
        server = create_mcp_server(gateway, _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            result = await client.call_tool("run_sql", {"sql": "SELECT * FROM api.about"})
            listing = {tool.name: tool for tool in (await client.list_tools()).tools}

        assert result.is_error is False
        assert result.structured_content == {"rows": [{"body": "Welcome to the memory."}], "note": None}
        declared = listing["run_sql"].output_schema or {}
        assert declared.get("type") == "object"
        # A scalar or list return would appear here as MCPServer's `{"result": ...}` wrapper instead.
        assert "result" not in (declared.get("properties") or {})

    async def test_a_date_cell_comes_back_as_a_json_string(self):
        """`SELECT id, updated FROM api.cards` is an ordinary query: its `updated` cell is a `date`.

        The gateway hands back the driver's own value, so the tool has to deliver a JSON scalar over
        the wire. A `date` that reached the wire unconverted failed the whole call.
        """
        rows = ({"id": "p1", "updated": date(2026, 9, 1)},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=("id", "updated")))
        server = create_mcp_server(gateway, _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            result = await client.call_tool("run_sql", {"sql": "SELECT id, updated FROM api.cards"})

        assert result.is_error is False
        assert result.structured_content == {"rows": [{"id": "p1", "updated": "2026-09-01"}], "note": None}

    async def test_appends_the_truncation_message(self):
        truncated = SqlResult(rows=({"id": "p1"},), columns=("id",), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(result=truncated)
        server = create_mcp_server(gateway, _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            result = await client.call_tool("run_sql", {"sql": "SELECT id FROM api.cards"})

        assert "50+ more rows — narrow your filters" in _text(result)

    async def test_db_error_fails_the_call_and_carries_the_actionable_read_only_message(self):
        """A query that did not run comes back as an ERROR, not as a result whose text says so.

        The `isError` assertion is the point: a message returned as a successful result is
        indistinguishable from an answer — a caller cannot tell a wrong query from an empty one, and
        a program carries the error text forward as data. The sandbox form raises for the same
        condition, and the two must agree.
        """
        gateway = _RaisingGateway()
        server = create_mcp_server(gateway, _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            result = await client.call_tool("run_sql", {"sql": "INSERT INTO kb.pages VALUES (1)"})

        assert result.is_error  # the call FAILED, it did not succeed with an apology
        text = _text(result)
        assert "read-only" in text  # the role is read-only
        assert "api." in text  # points the agent back at the api.* surface
        assert "log_session" not in text  # no write is offered, because none can run


class TestRunSqlVectorQuery:
    """`run_sql` embeds a `vector_query => '<text>'` pseudo-arg server-side (the hybrid fix)."""

    async def test_embeds_vector_query_and_inlines_a_halfvec_literal(self):
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)
        embedder = FakeEmbedding()
        server = create_mcp_server(gateway, _describe_handler(_schema()), _embed_query(embedder), collection=_COLLECTION)

        async with Client(server) as client:
            await client.call_tool(
                "run_sql", {"sql": "SELECT * FROM api.search('graph rag', vector_query => 'a richer description of the intent', k => 8)"}
            )

        assert embedder.texts == ["a richer description of the intent"]  # the semantic text was embedded, not the BM25 query
        ran = gateway.last.sql
        assert "vector_query" not in ran  # the pseudo-arg is gone
        assert "query_vec => '[" in ran  # replaced by a real inlined halfvec literal …
        assert "]'::halfvec" in ran  # … cast to halfvec
        assert "'graph rag'" in ran  # the lexical BM25 query is left untouched

    async def test_plain_sql_passes_through_verbatim_and_never_embeds(self):
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)
        embedder = FakeEmbedding()
        server = create_mcp_server(gateway, _describe_handler(_schema()), _embed_query(embedder), collection=_COLLECTION)

        async with Client(server) as client:
            await client.call_tool("run_sql", {"sql": "SELECT * FROM api.about"})

        assert embedder.texts == []  # no vector_query → the embedder is never touched
        assert gateway.last.sql == "SELECT * FROM (SELECT * FROM api.about) AS q LIMIT 51"  # the statement itself unchanged

    async def test_embedding_failure_degrades_to_bm25_with_a_note(self):
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)

        async def _boom(_text: str) -> list[float]:
            msg = "no az login"
            raise RuntimeError(msg)

        server = create_mcp_server(gateway, _describe_handler(_schema()), _boom, collection=_COLLECTION)

        async with Client(server) as client:
            result = await client.call_tool("run_sql", {"sql": "SELECT * FROM api.search('graph rag', vector_query => 'intent', k => 8)"})

        assert "vector_query" not in gateway.last.sql
        assert "query_vec => NULL" in gateway.last.sql  # degraded to the BM25-only RRF leg
        text = _text(result)
        assert "BM25" in text  # the agent is told it ran BM25-only …
        assert "no az login" in text  # … and why

    async def test_server_without_embed_capability_degrades_vector_query_to_bm25(self):
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)
        server = create_mcp_server(gateway, _describe_handler(_schema()), collection=_COLLECTION)  # no embed_query: a BM25-only deployment

        async with Client(server) as client:
            result = await client.call_tool("run_sql", {"sql": "SELECT * FROM api.search('graph rag', vector_query => 'intent')"})

        assert "query_vec => NULL" in gateway.last.sql
        assert "BM25" in _text(result)


class TestGraphToolRegistration:
    """The graph tools register under their names only when their handlers are wired by the host's wiring module."""

    async def test_graph_tools_register_when_their_handlers_are_provided(self):
        server = _graph_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        assert {"graph_expand", "find_paths"} <= names

    async def test_graph_tool_descriptions_are_non_empty(self):
        server = _graph_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        assert by_name["graph_expand"]
        assert by_name["find_paths"]

    async def test_graph_tools_absent_without_their_handlers(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        assert "graph_expand" not in names
        assert "find_paths" not in names


class TestGraphToolDescriptions:
    """The served `graph_expand` / `find_paths` reuse `tools.py`'s tuned text, extended for their richer args.

    Regression guard for the stale local copies the served descriptions used to be: they named the
    knowledge base's research vocabulary ("cards", "papers", "citation chains") and routed the agent at the
    SQL plane (`api.lookup_value`, `api.cards`) instead of a tool sitting in the same listing, and
    `find_paths`' empty-result guidance existed in `tools.py` but was never served. `_graph_server`
    wires no `search`, so the served text must ground every id from the task or a prior traversal
    instead — never from a tool this server does not hold.
    """

    async def test_graph_expand_grounds_ids_from_the_task_not_search(self):
        server = _graph_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        description = next(tool.description or "" for tool in listing.tools if tool.name == "graph_expand")
        assert "Use endpoint ids supplied with the task" in description
        assert "search" not in description
        assert "search" not in names  # no search handler is wired, so none is served
        assert "fetch(unit_id=" in description  # fetch IS wired here, so the description says so
        assert "fetch" in names  # and the listing has to carry the tool the description offers
        assert "api.lookup_value" not in description
        assert "api.cards" not in description
        assert "cards" not in description.lower()
        assert "paper" not in description.lower()

    async def test_graph_expand_documents_rel_types_and_k(self):
        server = _graph_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        description = next(tool.description or "" for tool in listing.tools if tool.name == "graph_expand")
        assert "`rel_types`" in description
        assert "`k`" in description
        assert "(default 20)" in description

    async def test_find_paths_grounds_ids_from_the_task_not_search(self):
        server = _graph_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        description = next(tool.description or "" for tool in listing.tools if tool.name == "find_paths")
        assert "Use endpoint ids supplied with the task" in description
        assert "search" not in description
        assert "search" not in names  # no search handler is wired, so none is served
        assert "fetch(unit_id=" in description  # fetch IS wired here, so the description says so
        assert "fetch" in names  # and the listing has to carry the tool the description offers
        assert "api.search" not in description
        assert "cards" not in description.lower()
        assert "citation" not in description.lower()

    async def test_find_paths_documents_max_hops_rel_types_and_k(self):
        server = _graph_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        description = next(tool.description or "" for tool in listing.tools if tool.name == "find_paths")
        assert "`max_hops`" in description
        assert "`rel_types`" in description
        assert "`k`" in description
        assert "(default 10)" in description

    async def test_find_paths_teaches_the_empty_result_exit(self):
        """The `tools.py` text this server now serves already carries the empty-result guidance."""
        server = _graph_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        description = next(tool.description or "" for tool in listing.tools if tool.name == "find_paths")
        assert "empty result" in description
        assert "graph_expand" in description


class TestGraphExpandTool:
    async def test_routes_a_graph_expand_message_with_the_tool_arguments(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        server = _graph_server(gateway)

        async with Client(server) as client:
            await client.call_tool(
                "graph_expand", {"seed_ids": ["paper.graph", "ent.bm25"], "hops": 3, "rel_types": ["cites", "evaluates"], "k": 15}
            )

        assert "api.graph_expand" in gateway.last.sql  # routed through GraphExpandHandler
        params = gateway.last.params
        assert params["seed_ids"] == ["paper.graph", "ent.bm25"]
        assert params["hops"] == 3
        assert params["rel_types"] == ["cites", "evaluates"]
        assert params["k"] == 15
        assert gateway.last.cap == _GRAPH_ROW_CAP  # k caps the NODES; the rows a node's assertions fill ride the graph cap

    async def test_an_empty_relation_list_reaches_what_omitting_it_reaches(self):
        """Naming no relation is the absence of a filter, so the two calls are one call.

        The answers match because the read underneath is the same read: an empty list binds as the
        unset argument does, which is what every backend's own SQL also reads it as.
        """
        rows = (
            _node_cells("paper.graph", type_="paper", title="Graph Indexing") | _edge_cells(None, None, None),
            _node_cells("ent.graphrag", title="GraphRAG") | _edge_cells("paper.graph", "ent.graphrag", "evaluates", "paper.graph#c1"),
        )
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        server = _graph_server(gateway)

        async with Client(server) as client:
            empty = await client.call_tool("graph_expand", {"seed_ids": ["paper.graph"], "rel_types": []})
            bound_empty = gateway.last.params["rel_types"]
            omitted = await client.call_tool("graph_expand", {"seed_ids": ["paper.graph"]})

        assert bound_empty is None, "an empty list reaches the traversal as the unset argument"
        assert gateway.last.params["rel_types"] is None
        empty_nodes = [node["id"] for node in (empty.structured_content or {})["nodes"]]
        omitted_nodes = [node["id"] for node in (omitted.structured_content or {})["nodes"]]
        assert empty_nodes == omitted_nodes == ["paper.graph", "ent.graphrag"]

    async def test_returns_subgraph_json_with_nodes_edges_and_provenance(self):
        rows = (
            _node_cells("paper.graph", type_="paper", title="Graph Indexing") | _edge_cells(None, None, None),  # seed: no in-edge
            _node_cells("ent.graphrag", title="GraphRAG") | _edge_cells("paper.graph", "ent.graphrag", "evaluates", "paper.graph#c1"),
        )
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        server = _graph_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("graph_expand", {"seed_ids": ["paper.graph"]})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert {node["id"] for node in payload["nodes"]} == {"paper.graph", "ent.graphrag"}
        (edge,) = payload["edges"]
        assert edge["src_id"] == "paper.graph"
        assert edge["dst_id"] == "ent.graphrag"
        assert edge["rel"] == "evaluates"
        assert edge["provenance_claim"] == "paper.graph#c1"  # provenance rides the edge


class TestGraphHandlesAreFetchable:
    """A graph answer hands back handles, not bare ids: every node and every edge's provenance reads back.

    The round trip is the claim behind an edge — the one thing a reader needs to verify a
    relationship — fetched by the exact `provenance_ref` the edge carried, with no id surgery.
    """

    _CLAIM: ClassVar[dict[str, object]] = {
        "id": "paper.graph#c1",
        "level": "claim",
        "title": "Claim",
        "body": "GraphRAG beats BM25 on multi-hop.",
    }

    def _server(self) -> MCPServer:
        """The full single-source surface with every handler on THIS server's own collection."""
        gateway = FakeSqlGateway(
            routed={
                "api.graph_expand": SqlResult(
                    rows=(
                        _node_cells("paper.graph", type_="paper") | _edge_cells(None, None, None),
                        _node_cells("ent.graphrag") | _edge_cells("paper.graph", "ent.graphrag", "evaluates", "paper.graph#c1"),
                    ),
                    columns=(),
                ),
                "api.fetch": SqlResult(rows=(self._CLAIM,), columns=()),
            }
        )
        return create_mcp_server(
            gateway,
            _describe_handler(_schema()),
            search_handler=SearchHandler(gateway, FakeEmbedding()),
            fetch_handler=FetchHandler(gateway, _COLLECTION),
            graph_expand_handler=GraphExpandHandler(_no_reach(gateway), _COLLECTION),
            find_paths_handler=GraphPathsHandler(gateway, _COLLECTION),
            envelope_handler=EnvelopeHandler(FakeSqlGateway(), collection=_COLLECTION),
            collection=_COLLECTION,
        )

    async def test_an_edges_provenance_ref_fetches_the_claim_that_asserts_it(self):
        async with Client(self._server()) as client:
            expanded = await client.call_tool("graph_expand", {"seed_ids": ["paper.graph"]})
            payload = json.loads(_text(expanded))
            assert {node["ref"] for node in payload["nodes"]} == {f"page:{_COLLECTION}/paper.graph", f"page:{_COLLECTION}/ent.graphrag"}
            (edge,) = payload["edges"]
            assert edge["provenance_ref"] == f"unit:{_COLLECTION}/paper.graph#c1"

            fetched = await client.call_tool("fetch", {"unit_id": edge["provenance_ref"]})

        assert not fetched.is_error, fetched.content
        claim = json.loads(_text(fetched))
        assert claim["id"] == "paper.graph#c1"
        assert claim["body"] == self._CLAIM["body"]


class TestFindPathsTool:
    async def test_routes_a_graph_paths_message_with_the_tool_arguments(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        server = _graph_server(gateway)

        async with Client(server) as client:
            await client.call_tool(
                "find_paths", {"from_id": "paper.graph", "to_id": "ent.bm25", "max_hops": 5, "rel_types": ["cites"], "k": 7}
            )

        (walk,) = (call for call in gateway.rows_calls if "api.graph_reach" not in call.sql)
        assert "api.graph_paths" in walk.sql  # routed through GraphPathsHandler
        params = walk.params
        assert params["from_id"] == "paper.graph"
        assert params["to_id"] == "ent.bm25"
        assert params["max_hops"] == 5
        assert params["rel_types"] == ["cites"]
        assert params["k"] == 7
        assert walk.cap == 8 * 6  # (k + 1) routes, each at most (max_hops + 1) nodes

    async def test_returns_ranked_paths_json(self):
        rows = (
            {"path_rank": 1, "score": 0.9} | _node_cells("paper.graph", type_="paper") | _edge_cells(None, None, None),
            {"path_rank": 1, "score": 0.9} | _node_cells("ent.bm25") | _edge_cells("paper.graph", "ent.bm25", "cites", "paper.graph#c2"),
            {"path_rank": 2, "score": 0.4} | _node_cells("paper.graph", type_="paper") | _edge_cells(None, None, None),
            {"path_rank": 2, "score": 0.4} | _node_cells("ent.bm25") | _edge_cells("paper.graph", "ent.bm25", "evaluates"),
        )
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        server = _graph_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("find_paths", {"from_id": "paper.graph", "to_id": "ent.bm25"})

        assert result.is_error is False
        paths = json.loads(_text(result))["paths"]
        assert len(paths) == 2  # two path_rank groups
        assert [node["id"] for node in paths[0]["nodes"]] == ["paper.graph", "ent.bm25"]
        assert paths[0]["score"] == 0.9
        assert paths[0]["edges"][0]["provenance_claim"] == "paper.graph#c2"


class TestSingleSourceRetrievalTools:
    """The structured `search` / `fetch` tools, defined once in `engram.retrieval.tools` and registered here.

    ``create_mcp_server`` builds a :class:`~engram.retrieval.tools.RetrievalHandlers` bundle from the
    wired handlers and calls :func:`~engram.retrieval.tools.register_mcp_tools`, so the in-process eval
    agent and this external MCP surface expose the *same* retrieval logic. The single-source bundle
    also carries `graph_expand` / `find_paths`, but the server keeps its own richer graph tools
    (`rel_types` / `k` / `max_hops`): registration is first-wins, so the rich ones survive and
    only `search` / `fetch` are added from the single source.
    """

    async def test_search_and_fetch_register_when_their_handlers_are_provided(self):
        server = _full_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        assert {"search", "fetch"} <= names

    async def test_search_and_fetch_descriptions_are_non_empty(self):
        server = _full_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        assert by_name["search"]
        assert by_name["fetch"]

    async def test_search_and_fetch_absent_without_their_handlers(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        assert "search" not in names
        assert "fetch" not in names

    async def test_keeps_run_sql_describe_schema_and_the_rich_graph_tools(self):
        gateway = FakeSqlGateway()
        server = _full_server(gateway)

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: tool for tool in listing.tools}
        assert {"describe_schema", "run_sql", "graph_expand", "find_paths"} <= set(by_name)
        # The single source also defines graph tools, but first-wins keeps the server's richer ones:
        # their arg surface still carries rel_types / k (the single-source versions lack them).
        expand_args = set(by_name["graph_expand"].input_schema.get("properties", {}))
        assert {"rel_types", "k"} <= expand_args  # the rich graph_expand, not the single-source one

    async def test_search_routes_through_the_search_handler_and_returns_hits(self):
        rows = ({"id": "paper.lightrag-2025", "hit_level": "card", "page_id": "paper.lightrag-2025", "snippet": "LightRAG", "score": 0.91},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        embedder = FakeEmbedding()
        server = _full_server(gateway, embedder)

        async with Client(server) as client:
            result = await client.call_tool("search", {"query": "graph rag", "k": 8})

        assert result.is_error is False
        assert "api.search" in gateway.last.sql  # routed through SearchHandler
        assert gateway.last.params["k"] == 8
        assert gateway.last.cap == 8  # k rides the row cap
        assert embedder.texts == ["graph rag"]  # hybrid by default → the query was embedded
        hits = json.loads(_text(result))["hits"]
        assert [hit["id"] for hit in hits] == ["paper.lightrag-2025"]
        assert hits[0]["score"] == 0.91

    async def test_search_bm25_only_skips_the_embedding(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        embedder = FakeEmbedding()
        server = _full_server(gateway, embedder)

        async with Client(server) as client:
            await client.call_tool("search", {"query": "graph rag", "bm25_only": True})

        assert embedder.texts == []  # bm25_only → no embedding
        assert gateway.last.params["qvec"] is None  # the BM25-only RRF leg

    async def test_search_refuses_another_partition_than_this_doors_own(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        server = _full_server(gateway)

        async with Client(server) as client:
            failed = await client.call_tool("search", {"query": "graph rag", "collection": "repo-docs"})

        assert failed.is_error is True
        assert repr(_COLLECTION) in _text(failed)
        assert all("api.search" not in call.sql for call in gateway.rows_calls)

    async def test_search_omitted_collection_keeps_this_doors_own_default(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        server = _full_server(gateway)

        async with Client(server) as client:
            await client.call_tool("search", {"query": "graph rag"})

        assert gateway.last.params["collection"] == _COLLECTION  # the collection this server was built over

    async def test_fetch_routes_through_the_fetch_handler_and_returns_the_unit(self):
        rows = ({"id": "paper.lightrag-2025#s2", "level": "section", "title": "Method", "body": "We index the knowledge base graph."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        server = _full_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("fetch", {"unit_id": "paper.lightrag-2025#s2"})

        assert result.is_error is False
        assert "api.fetch" in gateway.last.sql  # routed through FetchHandler
        assert gateway.last.params["unit_id"] == "paper.lightrag-2025#s2"
        unit = json.loads(_text(result))
        assert unit["id"] == "paper.lightrag-2025#s2"
        assert unit["body"] == "We index the knowledge base graph."

    async def test_fetch_unknown_id_fails_the_call_and_leaves_the_session_usable(self):
        """A not-found `fetch` is an ERROR that names the id -- and the session survives it.

        Returning `{"error": ...}` as a successful call would hand back an object shaped like a
        result but meaning failure — which the in-process form of this same tool never does (it
        raises `ModelRetry`). `ToolError` fails this one call while MCPServer keeps the session, which
        the second call below proves.
        """
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))  # no row → unknown id
        server = _full_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("fetch", {"unit_id": "paper.does-not-exist"})
            survived = await client.list_tools()  # the session is still usable after the failure

        assert result.is_error  # the call FAILED, it did not succeed with an error-shaped object
        assert "paper.does-not-exist" in _text(result)  # names the id so the agent re-grounds it
        assert "search" in {tool.name for tool in survived.tools}


class TestStamp:
    """A `stamp` given to `create_mcp_server` rides the `search` answer as `structuredContent.knowledge_base`."""

    _CODE_MODE_ONLY = frozenset({"search", "fetch", "graph_expand", "find_paths", "run_code"})

    async def test_search_structured_content_carries_the_stamp(self):
        rows = ({"id": "paper.lightrag-2025", "hit_level": "card", "page_id": "paper.lightrag-2025", "snippet": "LightRAG", "score": 0.91},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        stamp = KnowledgeBaseStamp(slug="repo-docs", changed_since=1)

        async def source() -> KnowledgeBaseStamp:
            return stamp

        server = _full_server(gateway, FakeEmbedding(), stamp=source)

        async with Client(server) as client:
            result = await client.call_tool("search", {"query": "graph rag"})

        assert result.is_error is False
        assert result.structured_content is not None
        assert result.structured_content["knowledge_base"]["slug"] == "repo-docs"

    async def test_a_sandboxed_search_answer_carries_the_stamp(self):
        rows = ({"id": "paper.lightrag-2025", "hit_level": "card", "page_id": "paper.lightrag-2025", "snippet": "LightRAG", "score": 0.91},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        stamp = KnowledgeBaseStamp(slug="repo-docs", changed_since=1)

        async def source() -> KnowledgeBaseStamp:
            return stamp

        server = _full_server(gateway, only=self._CODE_MODE_ONLY, stamp=source)

        code = "result = await search(query='graph rag', k=3)\nresult['knowledge_base']\n"
        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert payload["slug"] == "repo-docs"


class TestHostOwnedSearchCap:
    """`create_mcp_server(..., k_source=..., collection=_COLLECTION)` puts the search row cap in the host's hands, not the agent's.

    The config-driven door onto the same seam the in-process capability exposes: a host that pins the
    cap gets a `search` with no `k` argument at all, resolved afresh per call, and a description that
    never offers what the tool does not take. Wired nothing, the served surface is unchanged.
    """

    async def test_the_served_search_takes_no_k(self):
        server = _full_server(FakeSqlGateway(), k_source=lambda: 7)

        async with Client(server) as client:
            listing = await client.list_tools()

        [search] = [tool for tool in listing.tools if tool.name == "search"]
        assert "query" in search.input_schema["properties"]
        assert "k" not in search.input_schema["properties"]

    async def test_the_resolved_cap_reaches_the_search_handler(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        server = _full_server(gateway, k_source=lambda: 7)

        async with Client(server) as client:
            result = await client.call_tool("search", {"query": "graph rag"})

        assert result.is_error is False
        assert gateway.last.params["k"] == 7
        assert gateway.last.cap == 7

    async def test_a_cap_that_moves_lands_on_the_next_call(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        cap = {"k": 9}
        server = _full_server(gateway, k_source=lambda: cap["k"])

        async with Client(server) as client:
            await client.call_tool("search", {"query": "graph rag"})
            cap["k"] = 3
            await client.call_tool("search", {"query": "graph rag"})

        assert [recorded.params["k"] for recorded in gateway.rows_calls] == [9, 3]

    async def test_the_served_description_never_mentions_k(self):
        server = _full_server(FakeSqlGateway(), k_source=lambda: 7)

        async with Client(server) as client:
            listing = await client.list_tools()

        [search] = [tool for tool in listing.tools if tool.name == "search"]
        assert "`k`" not in (search.description or "")

    async def test_without_a_cap_the_served_search_is_unchanged(self):
        server = _full_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        [search] = [tool for tool in listing.tools if tool.name == "search"]
        assert search.input_schema["properties"]["k"]["default"] == 12


class TestDescriptionsOverride:
    """`create_mcp_server(..., descriptions=..., collection=_COLLECTION)` overrides the four retrieval tool descriptions by name (C9b).

    The tuned `tool_descriptions` reach the live MCP surface: `search` / `fetch` flow through the
    override seam in `register_mcp_tools`, and the server's own richer `graph_expand` / `find_paths` resolve
    the same fallback-to-default rule. `None` (the default) keeps every shipped description, so the
    read-only surface is unchanged.
    """

    async def test_descriptions_override_search_and_graph_expand_by_name(self):
        server = _full_server(FakeSqlGateway(), descriptions={"search": "TUNED SEARCH", "graph_expand": "TUNED EXPAND"})

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        assert by_name["search"] == "TUNED SEARCH"  # the single-source tool, via register_mcp_tools
        assert by_name["graph_expand"] == "TUNED EXPAND"  # the server's own rich graph tool

    async def test_descriptions_override_fetch_and_find_paths_by_name(self):
        server = _full_server(FakeSqlGateway(), descriptions={"fetch": "TUNED FETCH", "find_paths": "TUNED PATHS"})

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        assert by_name["fetch"] == "TUNED FETCH"
        assert by_name["find_paths"] == "TUNED PATHS"

    async def test_unnamed_tools_keep_their_shipped_descriptions(self):
        server = _full_server(FakeSqlGateway(), descriptions={"search": "ONLY SEARCH"})

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        # The other three retrieval tools (and the always-on pair) keep their non-empty shipped defaults.
        for name in ("fetch", "graph_expand", "find_paths", "describe_schema", "run_sql"):
            assert by_name[name], f"{name} should keep a non-empty shipped description"
            assert by_name[name] != "ONLY SEARCH", f"{name} must not pick up the search override"

    async def test_descriptions_none_keeps_every_shipped_default(self):
        overridden = _full_server(FakeSqlGateway(), descriptions=None)
        defaults = _full_server(FakeSqlGateway())

        async with Client(overridden) as client:
            overridden_by_name = {tool.name: tool.description for tool in (await client.list_tools()).tools}
        async with Client(defaults) as client:
            defaults_by_name = {tool.name: tool.description for tool in (await client.list_tools()).tools}

        assert overridden_by_name == defaults_by_name


class TestProfileGating:
    """`create_mcp_server` gates the four structured tools by the `only` set.

    `describe_schema` and `run_sql` are always present regardless of `only`; the four
    structured tools (`search`, `fetch`, `graph_expand`, `find_paths`) are registered iff
    `(only is None or name in only) AND the handler is present`.

    `only` is the raw gate: it registers exactly the names it is given, whatever a caller hands it,
    independent of any named strategy.

    Profile → expected structured tools:
    - `search-hybrid` → `only={"search","fetch"}` → {search, fetch}
    - `full` → `only={"search","fetch","graph_expand","find_paths"}` (= None) → all four
    - a graph-only slice → `only={"graph_expand","find_paths"}` → {graph_expand, find_paths} (the
      `graph-only` STRATEGY itself, `core.ai.retrieval.strategy.resolve_strategy("graph-only")`,
      resolves to `only={"graph_expand","find_paths","fetch"}`: a traversal an agent cannot read is
      not a usable profile)
    """

    _ALWAYS_ON = frozenset({"describe_schema", "run_sql"})

    def _all_handlers(self) -> tuple[FakeSqlGateway, SearchHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler]:
        gw = FakeSqlGateway()
        fetch, graph_expand, find_paths = FetchHandler(gw, "research"), GraphExpandHandler(_no_reach(gw), "research"), GraphPathsHandler(gw, "research")
        return gw, SearchHandler(gw, FakeEmbedding()), fetch, graph_expand, find_paths

    def _server_with_profile(self, only: frozenset[str] | None) -> MCPServer:
        _, sh, fh, geh, fph = self._all_handlers()
        return create_mcp_server(
            FakeSqlGateway(),
            _describe_handler(_schema()),
            search_handler=sh,
            fetch_handler=fh,
            graph_expand_handler=geh,
            find_paths_handler=fph,
            only=only,
            collection=_COLLECTION,
        )

    async def _tool_names(self, server: MCPServer) -> frozenset[str]:
        async with Client(server) as client:
            listing = await client.list_tools()
        return frozenset(tool.name for tool in listing.tools)

    async def test_search_hybrid_profile_exposes_only_search_and_fetch(self):
        server = self._server_with_profile(frozenset({"search", "fetch"}))

        names = await self._tool_names(server)

        assert names == self._ALWAYS_ON | {"search", "fetch"}

    async def test_full_profile_exposes_all_four_structured_tools(self):
        server = self._server_with_profile(frozenset({"search", "fetch", "graph_expand", "find_paths"}))

        names = await self._tool_names(server)

        assert names == self._ALWAYS_ON | {"search", "fetch", "graph_expand", "find_paths"}

    async def test_graph_only_profile_exposes_graph_tools_no_search_or_fetch(self):
        server = self._server_with_profile(frozenset({"graph_expand", "find_paths"}))

        names = await self._tool_names(server)

        assert names == self._ALWAYS_ON | {"graph_expand", "find_paths"}

    async def test_only_none_registers_all_four_structured_tools_when_handlers_present(self):
        # only=None is the "open surface" — equivalent to the full profile.
        server = self._server_with_profile(None)

        names = await self._tool_names(server)

        assert names == self._ALWAYS_ON | {"search", "fetch", "graph_expand", "find_paths"}

    async def test_describe_schema_and_run_sql_always_present_regardless_of_only(self):
        for only in (frozenset({"search", "fetch"}), frozenset({"graph_expand", "find_paths"}), frozenset()):
            server = self._server_with_profile(only)

            names = await self._tool_names(server)

            assert names >= self._ALWAYS_ON, f"always-on tools missing for only={only!r}"

    async def test_empty_only_set_exposes_no_structured_tools(self):
        server = self._server_with_profile(frozenset())

        names = await self._tool_names(server)

        # Only the always-on pair; no structured tools at all.
        assert names == self._ALWAYS_ON


class TestSwitchesGateTheServedGraphTools:
    """An auto-mode knowledge base's stored graph switch decides whether the server mounts the graph tools.

    The profile names all four read tools, as every profile the studio serves does. A switch that is
    off withholds `graph_expand` and `find_paths` from the top level and from the `run_code` sandbox.
    """

    _FULL = frozenset({"search", "fetch", "graph_expand", "find_paths"})
    _CODE_MODE = _FULL | {"run_code"}

    async def _listing(self, server: MCPServer) -> dict[str, str]:
        async with Client(server) as client:
            listing = await client.list_tools()
        return {tool.name: tool.description or "" for tool in listing.tools}

    async def test_an_off_graph_switch_withholds_the_graph_tools_the_profile_names(self):
        served = await self._listing(_full_server(FakeSqlGateway(), only=self._FULL, switches=RetrievalSwitches(graph=False)))

        assert {"search", "fetch"} <= set(served)
        assert "graph_expand" not in served
        assert "find_paths" not in served

    async def test_an_on_graph_switch_serves_them(self):
        served = await self._listing(_full_server(FakeSqlGateway(), only=self._FULL, switches=RetrievalSwitches(graph=True)))

        assert {"search", "fetch", "graph_expand", "find_paths"} <= set(served)

    async def test_an_off_graph_switch_keeps_the_graph_tools_out_of_the_sandbox(self):
        gateway = FakeSqlGateway()
        toolset = _build_sandbox_toolset(
            RetrievalHandlers(
                search=SearchHandler(gateway, FakeEmbedding()),
                fetch=FetchHandler(gateway, "research"),
                graph_expand=GraphExpandHandler(_no_reach(gateway), "research"),
                find_paths=GraphPathsHandler(gateway, "research"),
            ),
            gateway=gateway,
            embed_query=None,
            describe_schema=make_describe_schema(_describe_handler(_schema())),
            search_hybrid=None,
            switches=RetrievalSwitches(graph=False),
            collection=_COLLECTION,
        )

        assert set(toolset.tools) == {"search", "fetch", "run_sql", "describe_schema"}

    async def test_an_off_graph_switch_keeps_the_graph_tools_out_of_a_routed_sandbox(self):
        repo = FakeSqlGateway()
        toolset = _build_sandbox_toolset(
            RetrievalHandlers(
                search=SearchHandler(repo, FakeEmbedding()),
                fetch=FetchHandler(repo, "repo-docs"),
                graph_expand=GraphExpandHandler(_no_reach(repo), "repo-docs"),
                find_paths=GraphPathsHandler(repo, "repo-docs"),
            ),
            gateway=repo,
            embed_query=None,
            describe_schema=make_describe_schema(_describe_handler(_schema())),
            search_hybrid=False,
            switches=RetrievalSwitches(graph=False),
            collection="repo-docs",
            read_surfaces=(_routed_surface("repo-docs", repo, stamp=_fresh("repo-docs")),),
        )

        assert set(toolset.tools) == {"search", "fetch", "run_sql", "describe_schema"}

    async def test_an_off_graph_switch_drops_the_graph_leg_from_the_run_code_description(self):
        served = await self._listing(_full_server(FakeSqlGateway(), only=self._CODE_MODE, switches=RetrievalSwitches(graph=False)))

        assert "graph_expand" not in served["run_code"]
        assert "find_paths" not in served["run_code"]
        assert "search(query=..., k=12)" in served["run_code"]
        assert "run_sql(sql=..., row_limit=50)" in served["run_code"]

    async def test_without_switches_the_run_code_description_keeps_the_graph_leg(self):
        served = await self._listing(_full_server(FakeSqlGateway(), only=self._CODE_MODE))

        assert "graph_expand(seed_ids=..., hops=2)" in served["run_code"]


class TestGraphOnlyStrategyIsUsable:
    """`resolve_strategy("graph-only")` drives a real server that a traversal-only agent can act on.

    Before this profile carried `fetch`, a graph-only agent could name nodes and relations but never
    read one, so the traversal answered questions no downstream step could act on. Wired here with
    every handler present and `only=profile.tools`, the server must register `fetch` alongside the
    graph tools, never `search`, and every served description and next move must agree.
    """

    def _server(self, gw: FakeSqlGateway) -> MCPServer:
        profile = resolve_strategy("graph-only")
        return create_mcp_server(
            FakeSqlGateway(),
            _describe_handler(_schema()),
            search_handler=SearchHandler(gw, FakeEmbedding()),
            fetch_handler=FetchHandler(gw, "research"),
            graph_expand_handler=GraphExpandHandler(_no_reach(gw), "research"),
            find_paths_handler=GraphPathsHandler(gw, "research"),
            only=profile.tools,
            collection=_COLLECTION,
        )

    async def test_the_server_registers_fetch_and_the_graph_tools_never_search(self):
        server = self._server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        assert {"fetch", "graph_expand", "find_paths"} <= names
        assert "search" not in names

    async def test_every_served_description_grounds_ids_from_the_task_not_search(self):
        server = self._server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        for name in ("graph_expand", "find_paths"):
            assert "Use endpoint ids supplied with the task" in by_name[name]
            assert "search" not in by_name[name]
        assert "search" not in by_name["fetch"]

    async def test_an_expansion_offers_fetch_but_never_search_as_its_next_move(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=_graph_rows(1), columns=()))
        server = self._server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("graph_expand", {"seed_ids": ["paper.graph"]})

        options = " ".join(json.loads(_text(result))["next_steps"]["options"])
        assert "fetch(" in options
        assert "search(" not in options


class TestRoutedGraphOnlyStrategyIsTruthful:
    """`studio mcp` always wires `read_surfaces`, never the single-source path.

    `register_routed_mcp_tools` is the path `TestGraphOnlyStrategyIsUsable` does not cover. Under
    `graph-only`, the ROUTED `fetch`'s description and its refusals must be exactly as truthful as
    the single-source one: no mention of `search`, which this registration never serves.
    """

    def _server(self, gw: FakeSqlGateway, *, profile_name: str) -> MCPServer:
        """A server for `profile_name`, reachable through the SAME routed surface it registers."""
        profile = resolve_strategy(profile_name)
        surface = _routed_surface("research", gw, stamp=_fresh("research"))
        return create_mcp_server(
            FakeSqlGateway(),
            _describe_handler(_schema()),
            search_handler=SearchHandler(gw, FakeEmbedding()),
            fetch_handler=FetchHandler(gw, "research"),
            graph_expand_handler=GraphExpandHandler(_no_reach(gw), "research"),
            find_paths_handler=GraphPathsHandler(gw, "research"),
            envelope_handler=EnvelopeHandler(gw, collection="research", group_by="app"),
            only=profile.tools,
            search_hybrid=profile.search_hybrid,
            collection="research",
            read_surfaces=(surface,),
        )

    async def test_graph_only_registers_fetch_and_the_graph_tools_never_search(self):
        server = self._server(_research_gateway(), profile_name="graph-only")

        async with Client(server) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        assert {"fetch", "graph_expand", "find_paths"} <= names
        assert "search" not in names

    async def test_graph_only_routed_fetch_description_never_names_search(self):
        server = self._server(_research_gateway(), profile_name="graph-only")

        async with Client(server) as client:
            listing = await client.list_tools()

        fetch_description = next(tool.description or "" for tool in listing.tools if tool.name == "fetch")
        assert "`search` hits work too" not in fetch_description
        assert "supplied with the task" in fetch_description

    async def test_graph_only_routed_fetch_refuses_a_malformed_ref_without_naming_search(self):
        server = self._server(_research_gateway(), profile_name="graph-only")

        async with Client(server) as client:
            failed = await client.call_tool("fetch", {"unit_id": "page:"})

        assert failed.is_error
        message = _text(failed)
        assert "search" not in message
        assert "supplied with the task" in message

    async def test_full_profile_routed_fetch_description_still_teaches_search(self):
        server = self._server(_research_gateway(), profile_name="full")

        async with Client(server) as client:
            listing = await client.list_tools()

        fetch_description = next(tool.description or "" for tool in listing.tools if tool.name == "fetch")
        assert "`search`" in fetch_description


class TestCodeModeRegistration:
    """`run_code` is opt-in: present only when `"run_code" in only` and the retrieval handlers are wired.

    It collapses the retrieval round-trips into one sandboxed-Python call. It must stay off the
    default surface — only a profile that names it (the `code-mode` strategy) exposes it.
    """

    _CODE_MODE_ONLY = frozenset({"search", "fetch", "graph_expand", "find_paths", "run_code"})

    async def test_run_code_registers_when_named_in_only_and_handlers_present(self):
        server = _full_server(FakeSqlGateway(), only=self._CODE_MODE_ONLY)

        async with Client(server) as client:
            listing = await client.list_tools()

        assert "run_code" in {tool.name for tool in listing.tools}

    async def test_run_code_absent_from_the_default_full_surface(self):
        # The fully-open surface (only=None) is run_code-free — opt-in is preserved.
        server = _full_server(FakeSqlGateway())

        async with Client(server) as client:
            listing = await client.list_tools()

        assert "run_code" not in {tool.name for tool in listing.tools}

    async def test_run_code_absent_without_the_retrieval_handlers(self):
        # Even when named in `only`, run_code needs the retrieval handlers to build its sandbox.
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), only=self._CODE_MODE_ONLY, collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()

        assert "run_code" not in {tool.name for tool in listing.tools}

    async def test_run_code_description_lists_the_callable_functions(self):
        server = _full_server(FakeSqlGateway(), only=self._CODE_MODE_ONLY)

        async with Client(server) as client:
            listing = await client.list_tools()

        description = next(tool.description or "" for tool in listing.tools if tool.name == "run_code")
        # The sandbox binds the whole retrieval surface — incl. run_sql + describe_schema (decision 2).
        for fn in ("search", "fetch", "graph_expand", "find_paths", "run_sql", "describe_schema"):
            assert fn in description, f"run_code description should name {fn}"

    async def test_run_code_description_documents_the_structured_return_shape(self):
        server = _full_server(FakeSqlGateway(), only=self._CODE_MODE_ONLY)

        async with Client(server) as client:
            listing = await client.list_tools()

        description = next(tool.description or "" for tool in listing.tools if tool.name == "run_code")
        # The description and the runtime must agree: results are structured (dict-indexed), not text.
        assert "row['id']" in description  # dict-access ergonomics …
        assert "row.id" in description  # … called out against attribute-access

    async def test_run_code_description_uses_the_real_keyword_only_signatures(self):
        """The catalogue line must model the signature the sandbox actually binds — keyword-only, no `task`."""
        server = _full_server(FakeSqlGateway(), only=self._CODE_MODE_ONLY)

        async with Client(server) as client:
            listing = await client.list_tools()

        description = next(tool.description or "" for tool in listing.tools if tool.name == "run_code")
        assert "search(query=..., k=12)" in description
        assert "fetch(unit_id=...)" in description
        assert "no argument opens the knowledge base map" in description
        assert "keyword arguments only" in description
        assert "task=None" not in description  # `task` is not a parameter of the bound `search`
        assert "fetch(unit_id)" not in description  # the old, wrong (positional-looking) spelling

    async def test_run_code_description_drops_k_when_the_host_owns_the_cap(self):
        server = _full_server(FakeSqlGateway(), only=self._CODE_MODE_ONLY, k_source=lambda: 7)

        async with Client(server) as client:
            listing = await client.list_tools()

        description = next(tool.description or "" for tool in listing.tools if tool.name == "run_code")
        assert "search(query=...)" in description
        assert "search(query=..., k=12)" not in description
        assert "fetch(unit_id=...)" in description  # fetch's own signature is unaffected by the search cap


class TestCodeModeExecution:
    """`run_code` executes the model's Python in a Monty sandbox where the retrieval tools are bound."""

    _CODE_MODE_ONLY = frozenset({"search", "fetch", "graph_expand", "find_paths", "run_code"})

    async def test_runs_python_that_calls_a_bound_retrieval_function(self):
        rows = ({"id": "paper.lightrag-2025", "hit_level": "card", "page_id": "paper.lightrag-2025", "snippet": "LightRAG", "score": 0.91},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        server = _full_server(gateway, only=self._CODE_MODE_ONLY)

        # Tool returns are serialized to plain JSON-compatible data inside the sandbox (dicts, not
        # models), so the model indexes a hit with ['id'] — the ergonomics the description teaches.
        # `search` returns {"hits": [...], "next_steps": {...}}, so the code reads `result['hits']`.
        code = "result = await search(query='graph rag', k=3)\n[h['id'] for h in result['hits']]\n"
        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert result.is_error is False
        assert "api.search" in gateway.last.sql  # the sandboxed search routed through the real handler
        payload = json.loads(_text(result))
        assert payload == ["paper.lightrag-2025"]  # the last expression's value, serialized to JSON

    async def test_sandbox_run_sql_returns_structured_rows(self):
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)
        server = _full_server(gateway, only=self._CODE_MODE_ONLY)

        # In the sandbox run_sql yields list[dict] (not the rendered text table), so code indexes a
        # row by column — uniform with search/fetch &co. Here the last expression returns the rows.
        code = "rows = await run_sql(sql='SELECT body FROM api.about')\nrows\n"
        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert result.is_error is False
        assert gateway.last.sql == "SELECT * FROM (SELECT body FROM api.about) AS q LIMIT 51"  # run_sql is callable inside the sandbox
        assert gateway.last.cap == 50  # the same hard cap applies inside the sandbox
        rows = json.loads(_text(result))
        assert rows == [{"body": "Welcome to the memory."}]  # structured rows, not a rendered table

    async def test_sandbox_run_sql_rows_are_indexable_by_column(self):
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)
        server = _full_server(gateway, only=self._CODE_MODE_ONLY)

        # The point of structured rows: code can pull a cell out by name in the same round-trip.
        code = "rows = await run_sql(sql='SELECT body FROM api.about')\nrows[0]['body']\n"
        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert result.is_error is False
        assert json.loads(_text(result)) == "Welcome to the memory."

    async def test_sandbox_run_sql_embeds_a_vector_query(self):
        # The vector_query => '<text>' embed shim must survive into the sandboxed run_sql.
        gateway = FakeSqlGateway(result=_ABOUT_RESULT)
        embedder = FakeEmbedding()
        server = _full_server(gateway, embedder, only=self._CODE_MODE_ONLY)

        code = "await run_sql(sql=\"SELECT * FROM api.search('graph rag', vector_query => 'richer intent', k => 8)\")\n"
        async with Client(server) as client:
            await client.call_tool("run_code", {"code": code})

        assert embedder.texts == ["richer intent"]  # embedded server-side, inside the sandbox call
        assert "query_vec => '[" in gateway.last.sql  # rewritten to a real halfvec literal

    async def test_sandbox_run_sql_read_only_violation_surfaces_the_actionable_hint(self):
        # A read-only-role rejection raises ModelRetry inside the sandbox (not a fake-success row).
        # Unhandled by the model's code, it bubbles up as a Monty runtime error; _run_code returns
        # that rendered message as the tool result (the defensive contract — an actionable answer,
        # not a torn-down session), so the client sees the read-only hint rather than a crash.
        server = _full_server(_RaisingGateway(), only=self._CODE_MODE_ONLY)

        code = "await run_sql(sql='INSERT INTO kb.pages VALUES (1)')\n"
        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert "read-only" in _text(result)  # carries the actionable read-only hint through the sandbox

    async def test_sandbox_can_orchestrate_multiple_calls_in_one_round_trip(self):
        gateway = FakeSqlGateway(
            result=SqlResult(rows=({"id": "p1", "hit_level": "card", "page_id": "p1", "snippet": "s", "score": 0.5},), columns=())
        )
        server = _full_server(gateway, only=self._CODE_MODE_ONLY)

        # The whole point: N retrieval calls collapse into one MCP round-trip via asyncio.gather.
        code = "import asyncio\nresults = await asyncio.gather(search(query='a'), search(query='b'), search(query='c'))\nlen(results)\n"
        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert result.is_error is False
        assert json.loads(_text(result)) == 3

    async def test_sandbox_run_sql_delivers_driver_values_as_json_scalars(self):
        """The sandbox reads the same converted cells: a program indexes a date, a Decimal and a UUID as strings."""
        rows = ({"id": "p1", "updated": date(2026, 9, 1), "weight": Decimal("1.50"), "run": UUID("00000000-0000-4000-8000-000000000001")},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=("id", "updated", "weight", "run")))
        server = _full_server(gateway, only=self._CODE_MODE_ONLY)

        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": "await run_sql(sql='SELECT * FROM api.cards')\n"})

        assert result.is_error is False
        assert json.loads(_text(result)) == [
            {"id": "p1", "updated": "2026-09-01", "weight": "1.50", "run": "00000000-0000-4000-8000-000000000001"}
        ]

    async def test_sandbox_run_sql_receives_the_same_bounded_rows_as_the_top_level_form(self):
        """The sandbox's bare `list[dict]` and the top-level `{"rows": ..., "note": ...}` share one bounding core."""
        wide_row = {f"col{i}": "z" * 2_500 for i in range(10)}
        rows = (
            *({"id": f"row{i}", "value": i} for i in range(10)),
            {"id": "wide", **wide_row},
        )
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=("id", "value", *wide_row)))
        server = _full_server(gateway, only=self._CODE_MODE_ONLY)

        async with Client(server) as client:
            top_level = await client.call_tool("run_sql", {"sql": "SELECT * FROM api.cards"})
            sandboxed = await client.call_tool("run_code", {"code": "await run_sql(sql='SELECT * FROM api.cards')\n"})

        assert top_level.is_error is False
        assert sandboxed.is_error is False
        top_rows = (top_level.structured_content or {})["rows"]
        sandbox_rows = json.loads(_text(sandboxed))
        assert sandbox_rows == top_rows  # identical bounded rows, even though the sandbox carries no note


class TestCodeModeLeveledFetch:
    """When an `EnvelopeHandler` is wired, the sandboxed `fetch` is the SAME leveled read the top-level tool serves.

    Regression guard for the sandbox being a second, contradictory retrieval world: without
    threading `envelope=` into the sandbox toolset, `run_code`'s `fetch` stayed the flat legacy
    read (`{id, level, title, body}`, `unit_id` required) even while the standalone `fetch` tool on
    the very same server was leveled (`{ref, page_type, title, summary, content, children, mode}`,
    no-argument default). One server, one `fetch` meaning.
    """

    _CODE_MODE_ONLY = frozenset({"search", "fetch", "graph_expand", "find_paths", "run_code"})

    def _envelope_handler(self, envelope_result: SqlResult) -> EnvelopeHandler:
        # The handler is built over the same collection `_full_server` threads into
        # `create_mcp_server`, so the sandbox's default ref and the handler's ownership check
        # agree, exactly as production wiring keeps them in sync by threading one spec value
        # into both.
        return EnvelopeHandler(FakeSqlGateway(routed={"envelope_": envelope_result}), collection=_COLLECTION)

    async def test_sandboxed_fetch_with_no_argument_opens_the_knowledge_base_map(self):
        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "S."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        envelope_handler = self._envelope_handler(SqlResult(rows=rows, columns=()))
        server = _full_server(gateway, only=self._CODE_MODE_ONLY, envelope_handler=envelope_handler)

        code = "env = await fetch()\nenv\n"
        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert payload["ref"] == f"collection:{_COLLECTION}"
        assert "children" in payload  # the leveled envelope shape, not the flat {id, level, title, body}

    async def test_sandboxed_fetch_matches_the_leveled_shape_not_the_flat_one(self):
        page_rows = (
            {"row_type": "page", "id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "S."},
            {"row_type": "section", "id": "mu.x#s1", "page_type": "section", "title": "S1", "summary": ""},
        )
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        envelope_handler = self._envelope_handler(SqlResult(rows=page_rows, columns=()))
        server = _full_server(gateway, only=self._CODE_MODE_ONLY, envelope_handler=envelope_handler)

        code = "env = await fetch(unit_id='mu.x')\nenv\n"
        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert payload["ref"] == "page:workdocs/mu.x"
        assert set(payload) == {"ref", "kind", "title", "summary", "content", "children", "mode", "next_steps", "knowledge_base"}

    async def test_without_an_envelope_handler_the_sandboxed_fetch_stays_flat(self):
        rows = ({"id": "mu.x", "level": "page", "title": "X", "body": "Body of X."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        server = _full_server(gateway, only=self._CODE_MODE_ONLY)  # no envelope_handler

        code = "unit = await fetch(unit_id='mu.x')\nunit\n"
        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert set(payload) == {"id", "level", "title", "body", "knowledge_base", "ref"}  # the flat shape, with the knowledge base stamp slot every fetch answer carries


class TestCodeModeStandaloneDriveContract:
    """Pin the pydantic-ai APIs `mcp_server._run_code` drives `CodeModeToolset` with, standalone.

    ``_run_code`` runs the sandbox **outside** an :class:`~pydantic_ai.Agent` by hand-building a
    :class:`~pydantic_ai.RunContext`, assigning ``ctx.tool_manager``, and walking
    ``for_run`` → enter the bound copy → ``get_tools``/``call_tool`` → :class:`~pydantic_ai.messages.ToolReturn`. That is a
    private contract with pydantic-ai's internals (verified against the installed pydantic-ai + harness pair). This test exercises each
    step directly so a version bump that renames/moves any of them fails *here* with a clear message —
    rather than only surfacing as a confusing failure inside the end-to-end execution tests.
    """

    async def test_run_code_standalone_drive_contract(self):
        # The exact imports + sequence _run_code uses (see engram/retrieval/infrastructure/mcp_server.py::_run_code).
        # A TestModel stands in for the (never-invoked) RunContext.model — _run_code uses engram's own
        # _NoModel sentinel, but this test pins the *API shape* RunContext requires, not that instance.
        from pydantic_ai import FunctionToolset, RunContext
        from pydantic_ai.messages import ToolReturn
        from pydantic_ai.models.test import TestModel
        from pydantic_ai.tool_manager import ToolManager
        from pydantic_ai.usage import RunUsage
        from pydantic_ai_harness.code_mode import CodeModeToolset

        async def ping(value: str) -> dict[str, str]:
            return {"echo": value}

        inner: FunctionToolset[None] = FunctionToolset()
        inner.add_function(ping, takes_ctx=False, name="ping")
        code_mode_toolset = CodeModeToolset[None](wrapped=inner)

        # 1) RunContext constructs with exactly the kwargs _run_code passes (deps/model/usage/tool_call_id).
        ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage(), tool_call_id="contract")

        # 2) ctx.tool_manager is an assignable field, and ToolManager(toolset=...) constructs.
        ctx.tool_manager = ToolManager(toolset=code_mode_toolset)
        assert ctx.tool_manager is not None, "RunContext.tool_manager must be assignable (CodeModeToolset.call_tool asserts it)"

        # 3) for_run returns a fresh CodeModeToolset (isolated REPL state per call).
        bound = await code_mode_toolset.for_run(ctx)
        assert isinstance(bound, CodeModeToolset), "CodeModeToolset.for_run must return a CodeModeToolset"

        # 4) The BOUND copy is an async context manager, and entering it is what allocates the
        #    Monty run state — `call_tool` asserts it (for_run does not inherit an entered state).
        async with bound:
            # 5) get_tools exposes the run_code meta-tool by the name we dispatch.
            tools = await bound.get_tools(ctx)
            assert "run_code" in tools, "CodeModeToolset.get_tools must expose a 'run_code' tool"

            # 6) call_tool runs the sandboxed code and returns a ToolReturn whose return_value is the result.
            result = await bound.call_tool("run_code", {"code": "await ping(value='hi')\n"}, ctx, tools["run_code"])
        assert isinstance(result, ToolReturn), "CodeModeToolset.call_tool must return a ToolReturn"
        assert result.return_value == {"echo": "hi"}, "ToolReturn.return_value must carry the last expression's value"


def _doc_authoring_server(tmp_path: Path) -> tuple[MCPServer, FakeReindex]:
    """A server with the documentation-authoring bundle mounted over one real `AuthoringTools` knowledge_base."""
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    knowledge_base = AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=research_spec())
    reindex = FakeReindex()
    ledger = SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=claimants_for(("intake",)))
    doors = AuthoringTools(knowledge_bases={"research": knowledge_base}, reindex=reindex, admin=FakeAdminGateway(), queue_for=lambda _t: ledger)
    server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), doc_authoring_tools=doors, collection=_COLLECTION)
    return server, reindex


class _RecordingWriteNoteDoor(AuthoringTools):
    """A minimal `AuthoringTools` stand-in that only records `write_note`'s arguments, writing nothing."""

    def __init__(self) -> None:
        self.write_note_calls: list[tuple[str, dict[str, object], str]] = []

    async def write_note(self, knowledge_base: str, payload: dict[str, object], path: str = "") -> str:
        self.write_note_calls.append((knowledge_base, payload, path))
        return "written"


class TestDocAuthoringMount:
    """`create_mcp_server(doc_authoring_tools=..., collection=_COLLECTION)` mounts the six documentation-authoring tools."""

    async def test_write_note_forwards_path_to_the_door(self):
        door = _RecordingWriteNoteDoor()
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), doc_authoring_tools=door, collection=_COLLECTION)

        async with Client(server) as client:
            result = await client.call_tool(
                "write_note", {"knowledge_base": "research", "payload": {"id": "x"}, "path": "docs/initiatives/2026-09-01-x/design.md"}
            )

        assert result.is_error is False
        assert door.write_note_calls == [("research", {"id": "x"}, "docs/initiatives/2026-09-01-x/design.md")]

    async def test_the_six_tools_are_mounted(self, tmp_path: Path):
        server, _ = _doc_authoring_server(tmp_path)

        async with Client(server) as client:
            listing = await client.list_tools()

        expected = {"knowledge_base_profile", "skeleton", "write_note", "decide_page", "resolve_entities", "read_page"}
        assert expected <= {tool.name for tool in listing.tools}

    async def test_read_page_forwards_the_call_and_answers_the_refusal(self, tmp_path: Path):
        server, reindex = _doc_authoring_server(tmp_path)

        async with Client(server) as client:
            result = await client.call_tool("read_page", {"knowledge_base": "research", "page_id": "flag.try-planning-reminders"})

        assert result.is_error
        assert "no indexed page 'flag.try-planning-reminders' in knowledge base 'research'" in _text(result)
        assert reindex.knowledge_bases == []

    async def test_decide_page_forwards_the_call_and_reindexes(self, tmp_path: Path):
        server, reindex = _doc_authoring_server(tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(
                "decide_page", {"knowledge_base": "research", "page_id": "flag.try-planning-reminders", "status": "dismissed"}
            )

        assert result.is_error is False
        assert "flag.try-planning-reminders (flag) is now dismissed in research" in _text(result)
        assert "status: dismissed" in (tmp_path / "Wiki" / "Flags" / "try-planning-reminders.md").read_text(encoding="utf-8")
        assert [knowledge_base.name for knowledge_base in reindex.knowledge_bases] == ["research"]

    async def test_decide_page_with_no_status_writes_the_links_and_keeps_the_status(self, tmp_path: Path):
        server, _ = _doc_authoring_server(tmp_path)

        async with Client(server) as client:
            listing = await client.list_tools()
            result = await client.call_tool(
                "decide_page", {"knowledge_base": "research", "page_id": "flag.try-planning-reminders", "links": {"resolved_by": ["q.planning"]}}
            )

        decide = next(tool for tool in listing.tools if tool.name == "decide_page")
        assert "status" not in decide.input_schema.get("required", [])
        assert "empty status keeps the page's current status" in (decide.description or "")
        assert "add" in decide.input_schema["properties"]
        assert "add" not in decide.input_schema.get("required", [])
        assert result.is_error is False
        assert "flag.try-planning-reminders (flag) stays pending in research" in _text(result)
        text = (tmp_path / "Wiki" / "Flags" / "try-planning-reminders.md").read_text(encoding="utf-8")
        assert "status: pending" in text
        assert 'resolved_by: ["[[q.planning]]"]' in text

    async def test_decide_page_with_add_merges_the_links_into_the_field(self, tmp_path: Path):
        server, _ = _doc_authoring_server(tmp_path)
        page = tmp_path / "Wiki" / "Flags" / "try-planning-reminders.md"

        async with Client(server) as client:
            await client.call_tool("decide_page", {"knowledge_base": "research", "page_id": "flag.try-planning-reminders", "links": {"resolved_by": ["q.old"]}})
            result = await client.call_tool(
                "decide_page",
                {"knowledge_base": "research", "page_id": "flag.try-planning-reminders", "links": {"resolved_by": ["q.new"]}, "add": True},
            )

        assert result.is_error is False
        assert 'resolved_by: ["[[q.old]]", "[[q.new]]"]' in page.read_text(encoding="utf-8")

    async def test_decide_page_of_an_unknown_knowledge_base_names_the_known_ones(self, tmp_path: Path):
        server, _ = _doc_authoring_server(tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(
                "decide_page", {"knowledge_base": "not-a-real-knowledge_base", "page_id": "flag.try-planning-reminders", "status": "accepted"}
            )

        assert result.is_error
        assert "research" in _text(result)  # the valid knowledge base names, so the agent can retry correctly

    async def test_resolve_entities_forwards_the_names_and_answers_the_refusal(self, tmp_path: Path):
        server, reindex = _doc_authoring_server(tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(
                "resolve_entities", {"knowledge_base": "research", "entities": [{"name": "Kestrel Labs", "entity_type": "tool", "aliases": ["Kestrel"]}]}
            )

        assert result.is_error
        assert "research keeps no entity pages" in _text(result)
        assert reindex.knowledge_bases == []


class _RaisingGateway(FakeSqlGateway):
    """A :class:`FakeSqlGateway` whose ``rows`` raises a DB error (the read-only-role path)."""

    async def rows(self, sql: str, params=None, *, cap: int = 50) -> SqlResult:
        msg = "cannot execute INSERT in a read-only transaction"
        raise DBAPIError(statement=sql, params=None, orig=Exception(msg))


class TestServerInstructions:
    """`create_mcp_server` always situates the agent in the collection landscape, tuned text riding alongside."""

    def test_passed_instructions_still_land_on_the_server(self):
        server = create_mcp_server(
            FakeSqlGateway(), _describe_handler(_schema()), instructions="TUNED INSTRUCTIONS", collection=_COLLECTION
        )

        assert server.instructions is not None
        assert "TUNED INSTRUCTIONS" in server.instructions

    def test_the_served_collection_is_named_as_the_default(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        assert server.instructions is not None
        assert _COLLECTION in server.instructions  # the collection this server was built over

    def test_a_scoped_collection_is_named_as_the_default(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection="repo-docs")

        assert server.instructions is not None
        assert "repo-docs" in server.instructions

    def test_the_paragraph_points_at_api_about(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(_schema()), collection=_COLLECTION)

        assert server.instructions is not None
        assert "api.about" in server.instructions


# -- routed reads over several registered sources -----------------------------

_RESEARCH_PAGE_REF = "page:research/paper.graph"
_RESEARCH_SECTION_REF = "section:research/paper.graph#findings"
_RESEARCH_CLAIM_REF = "unit:research/paper.graph#c1"


def _research_gateway() -> FakeSqlGateway:
    """A research knowledge base whose map groups by app and whose ungrouped bucket overflows the row cap."""
    page_stub = {"id": "paper.graph", "page_type": "reference", "title": "Graph", "summary": "Graph evidence"}
    section = {"id": "paper.graph#findings", "page_type": "section", "title": "Findings", "summary": "Evidence", "body": "research findings"}
    overflowing = SqlResult(
        rows=tuple({**page_stub, "id": f"paper.filler-{index}"} for index in range(50)),
        columns=(),
        truncated_message="50+ more rows — narrow your filters",
    )
    return FakeSqlGateway(
        routed={
            "api.search": SqlResult(
                rows=(
                    {"id": "paper.graph", "hit_level": "card", "page_title": "Graph", "snippet": "graph evidence", "score": 1.0},
                    {"id": "paper.graph#c1", "hit_level": "claim", "page_title": "Graph", "snippet": "a claim", "score": 0.5},
                ),
                columns=(),
            ),
            "api.fetch": SqlResult(
                rows=({"id": "paper.graph#c1", "level": "claim", "title": "Claim", "body": "research claim"},), columns=()
            ),
            "api.collection_about": SqlResult(rows=({"body": "# Purpose & scope\n\nResearch."},), columns=()),
            "api.envelope_collection_groups": SqlResult(rows=({"app": "", "pages": 51, "sample_titles": ["Graph"]},), columns=()),
            "api.envelope_group_page_types(": SqlResult(rows=({"page_type": "reference", "pages": 51, "sample_titles": ["Graph"]},), columns=()),
            "api.envelope_group_page_type(": SqlResult(rows=(page_stub,), columns=()),
            "api.envelope_group(": overflowing,
            "api.envelope_page(": SqlResult(rows=({**page_stub, "row_type": "page"}, {**section, "row_type": "section"}), columns=()),
            "api.envelope_section(": SqlResult(rows=(section,), columns=()),
        }
    )


def _routed_surface(name: str, gateway: FakeSqlGateway, *, stamp: StampSource) -> ReadSurface:
    """One registered read surface over `gateway`, reading and disclosing as its own collection."""
    return ReadSurface(
        name=name,
        collection=name,
        domain=f"The {name} knowledge base.",
        hybrid=False,
        stamp=stamp,
        handlers=RetrievalHandlers(
            search=SearchHandler(gateway, FakeEmbedding()),
            fetch=FetchHandler(gateway, name),
            graph_expand=GraphExpandHandler(_no_reach(gateway), name),
            find_paths=GraphPathsHandler(gateway, name),
        ),
        envelope=EnvelopeHandler(gateway, collection=name, group_by="app"),
    )


def _opened(result: CallToolResult) -> FetchEnvelope:
    """One leveled `fetch` answer, read off the wire and validated against the shape it promises.

    `fetch` returns either shape, so MCPServer nests its `structuredContent` under `result`. The text
    block carries the same payload unnested, and that is what the two `_opened`/`_fetched` readers
    parse.
    """
    assert not result.is_error, result.content
    payload = json.loads(_text(result))
    assert result.structured_content == {"result": payload}
    return FetchEnvelope.model_validate(payload)


def _fetched(result: CallToolResult) -> FetchUnit:
    """One flat `fetch` answer, read off the wire the same way `_opened` reads a leveled one."""
    assert not result.is_error, result.content
    return FetchUnit.model_validate(json.loads(_text(result)))


def _ranked(result: CallToolResult) -> SearchResult:
    """One `search` answer: a single model, so MCPServer publishes it as `structuredContent` unnested."""
    assert not result.is_error, result.content
    payload = json.loads(_text(result))
    assert result.structured_content == payload
    return SearchResult.model_validate(payload)


def _fresh(slug: str) -> StampSource:
    """A host stamp naming `slug` with its indexed copy measured and current."""

    async def read() -> KnowledgeBaseStamp:
        return KnowledgeBaseStamp(slug=slug, changed_since=0)

    return read


class TestRoutedReadsKeepEveryHandle:
    """Several registered sources behind one `search`/`fetch`: the handle that comes back is the handle that reads.

    `repo-docs` and `research` hold the same bare page id, so every assertion here is about which
    source answered, never about which id was asked for. `remote` is the host-supplied source with
    no measurable freshness, and `unregistered` is a collection the database may well hold but this
    server was never given.
    """

    def _server(self, research: FakeSqlGateway, repo: FakeSqlGateway, remote: FakeSqlGateway) -> MCPServer:
        server = MCPServer("routed-reads")
        register_routed_mcp_tools(
            server,
            (
                _routed_surface("repo-docs", repo, stamp=_fresh("repo-docs")),
                _routed_surface("research", research, stamp=_fresh("research")),
                _routed_surface("remote", remote, stamp=slug_only_stamp("remote")),
            ),
            default_knowledge_base="repo-docs",
        )
        return server

    async def test_a_search_hits_ref_reads_the_page_then_its_section_verbatim(self):
        research, repo, remote = _research_gateway(), FakeSqlGateway(), FakeSqlGateway()

        async with Client(self._server(research, repo, remote)) as client:
            searched = _ranked(await client.call_tool("search", {"query": "graph evidence", "collection": "research"}))
            hit_ref = searched.hits[0].ref
            assert hit_ref == _RESEARCH_PAGE_REF
            assert searched.knowledge_base is not None
            assert searched.knowledge_base.slug == "research"
            assert hit_ref in str(searched.next_steps)

            page = _opened(await client.call_tool("fetch", {"unit_id": hit_ref}))
            assert page.ref == hit_ref
            assert page.knowledge_base == KnowledgeBaseStamp(slug="research", changed_since=0)
            assert "research findings" in str(page.content)
            section_ref = page.children[0].ref
            assert section_ref == _RESEARCH_SECTION_REF
            assert section_ref in str(page.next_steps)

            section = _opened(await client.call_tool("fetch", {"unit_id": section_ref}))
            assert section.ref == section_ref
            assert section.knowledge_base == KnowledgeBaseStamp(slug="research", changed_since=0)
            assert hit_ref in str(section.next_steps)

        assert repo.rows_calls == [], "the base that was not named reads nothing"

    async def test_a_flat_units_ref_comes_back_as_itself_with_its_own_stamp(self):
        research, repo, remote = _research_gateway(), FakeSqlGateway(), FakeSqlGateway()

        async with Client(self._server(research, repo, remote)) as client:
            searched = _ranked(await client.call_tool("search", {"query": "graph evidence", "collection": "research"}))
            claim_ref = searched.hits[1].ref
            assert claim_ref == _RESEARCH_CLAIM_REF

            claim = _fetched(await client.call_tool("fetch", {"unit_id": claim_ref}))
            assert claim.ref == claim_ref
            assert claim.body == "research claim"
            assert claim.knowledge_base == KnowledgeBaseStamp(slug="research", changed_since=0)

        assert repo.rows_calls == []

    async def test_the_map_descends_group_by_group_without_ever_losing_its_collection(self):
        research, repo, remote = _research_gateway(), FakeSqlGateway(), FakeSqlGateway()

        async with Client(self._server(research, repo, remote)) as client:
            stamp = KnowledgeBaseStamp(slug="research", changed_since=0)
            root = _opened(await client.call_tool("fetch", {"unit_id": "collection:research"}))
            assert root.ref == "collection:research"
            assert root.knowledge_base == stamp
            group_ref = root.children[0].ref
            assert group_ref == "group:research/"

            group = _opened(await client.call_tool("fetch", {"unit_id": group_ref}))
            assert group.ref == group_ref
            assert group.knowledge_base == stamp
            nested_ref = group.children[0].ref
            assert nested_ref == "group:research//reference"

            nested = _opened(await client.call_tool("fetch", {"unit_id": nested_ref}))
            assert nested.ref == nested_ref
            assert nested.knowledge_base == stamp
            page_ref = nested.children[0].ref
            assert page_ref == _RESEARCH_PAGE_REF

            page = _opened(await client.call_tool("fetch", {"unit_id": page_ref}))
            assert page.ref == page_ref
            assert page.knowledge_base == stamp

        assert repo.rows_calls == []

    async def test_a_legacy_local_ref_comes_back_under_the_default_base(self):
        research, repo, remote = _research_gateway(), _research_gateway(), FakeSqlGateway()

        async with Client(self._server(research, repo, remote)) as client:
            page = _opened(await client.call_tool("fetch", {"unit_id": "page:paper.graph"}))
            assert page.ref == "page:repo-docs/paper.graph"
            assert page.children[0].ref == "section:repo-docs/paper.graph#findings"

            section = _opened(await client.call_tool("fetch", {"unit_id": "section:paper.graph#findings"}))
            assert section.ref == "section:repo-docs/paper.graph#findings"

        assert research.rows_calls == [], "an unqualified handle never reaches a base the caller did not name"

    async def test_a_source_with_no_measurable_freshness_says_so_rather_than_guessing(self):
        research, repo, remote = _research_gateway(), FakeSqlGateway(), FakeSqlGateway()

        async with Client(self._server(research, repo, remote)) as client:
            searched = _ranked(await client.call_tool("search", {"query": "anything", "collection": "remote"}))

        assert searched.knowledge_base == KnowledgeBaseStamp(slug="remote", changed_since=None)

    async def test_an_unregistered_collection_is_refused_naming_the_registered_ones(self):
        research, repo, remote = _research_gateway(), FakeSqlGateway(), FakeSqlGateway()

        async with Client(self._server(research, repo, remote)) as client:
            refused = await client.call_tool("search", {"query": "graph", "collection": "unregistered"})
            rejected_read = await client.call_tool("fetch", {"unit_id": "page:unregistered/paper.graph"})

        assert refused.is_error
        for registered in ("repo-docs", "research", "remote"):
            assert registered in _text(refused)
        assert rejected_read.is_error
        assert "unregistered" in _text(rejected_read)
        assert research.rows_calls == []
        assert repo.rows_calls == []
        assert remote.rows_calls == []

    async def test_the_tool_listing_teaches_the_collection_argument_and_the_returned_ref(self):
        research, repo, remote = _research_gateway(), FakeSqlGateway(), FakeSqlGateway()

        async with Client(self._server(research, repo, remote)) as client:
            listing = await client.list_tools()

        by_name = {tool.name: tool for tool in listing.tools}
        assert set(by_name) == {"search", "fetch"}
        search_description = by_name["search"].description or ""
        assert "one of: repo-docs, research, remote" in search_description
        assert "`ref` to `fetch` exactly as returned" in search_description, "a bare id no longer says which base to open it in"
        assert "api.about" not in search_description, "a collection this server does not serve is not on offer"
        assert "collection" in by_name["search"].input_schema["properties"]
        assert by_name["search"].input_schema["properties"]["k"]["default"] == 12
        fetch_description_text = by_name["fetch"].description or ""
        assert "repo-docs, research, remote" in fetch_description_text
        assert "map" in fetch_description_text, "the leveled read still advertises the served base's map"
        assert by_name["fetch"].input_schema["properties"]["unit_id"]["default"] == "collection:repo-docs"

    async def test_a_tuned_description_still_names_the_registered_set_and_the_ref(self):
        """An operator's own wording replaces the shipped text; it cannot replace what routing changed.

        A tuned description that says nothing about the closed `collection` set, or about the `ref`
        carrying its own knowledge base, leaves the agent reaching for a bare id — which reads this
        server's own base and quietly answers from the wrong one.
        """
        research, repo, remote = _research_gateway(), FakeSqlGateway(), FakeSqlGateway()
        server = MCPServer("tuned-routed-reads")
        register_routed_mcp_tools(
            server,
            (
                _routed_surface("repo-docs", repo, stamp=_fresh("repo-docs")),
                _routed_surface("research", research, stamp=_fresh("research")),
                _routed_surface("remote", remote, stamp=slug_only_stamp("remote")),
            ),
            default_knowledge_base="repo-docs",
            descriptions={"search": "TUNED SEARCH", "fetch": "TUNED FETCH"},
        )

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        for name, tuned in (("search", "TUNED SEARCH"), ("fetch", "TUNED FETCH")):
            assert tuned in by_name[name], "the operator's own wording still stands"
            assert "repo-docs, research, remote" in by_name[name]
        assert "No other collection is served here" in by_name["search"]
        assert "`ref` to `fetch` exactly as returned" in by_name["search"]
        assert "already names its knowledge base" in by_name["fetch"]

    async def test_only_gates_both_tools_alike(self):
        """A profile naming one of the two registers one of the two, `fetch` on the same terms as `search`.

        No shipped strategy drops `fetch` today, so this pins the gate rather than a live profile: a
        server whose profile excludes a tool must not serve it anyway.
        """
        server = MCPServer("search-only-routed-reads")
        register_routed_mcp_tools(
            server,
            (_routed_surface("research", _research_gateway(), stamp=_fresh("research")),),
            default_knowledge_base="research",
            only=frozenset({"search"}),
        )

        async with Client(server) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        assert names == {"search"}

    async def test_two_surfaces_claiming_one_collection_are_refused_outright(self):
        """A ref names a collection, so a second claimant on it could only ever be shadowed."""
        server = MCPServer("colliding-reads")

        with pytest.raises(ValueError, match="research"):
            register_routed_mcp_tools(
                server,
                (
                    _routed_surface("research", _research_gateway(), stamp=_fresh("research")),
                    _routed_surface("research", FakeSqlGateway(), stamp=_fresh("research")),
                ),
                default_knowledge_base="research",
            )


class TestRoutedReadsReachInsideTheSandbox:
    """`run_code` reads the SAME registered knowledge bases the standalone tools do.

    The defect this guards: a sandbox wired single-source while the server's own `search`/`fetch`
    were routed. `search(collection='research')` inside the program then ranked research through
    this server's own gateway, qualified the hits as research, stamped them with THIS base's
    freshness, and the sandbox's own `fetch` refused the ref it had just been handed.
    """

    _CODE_MODE_ONLY = frozenset({"search", "fetch", "graph_expand", "find_paths", "run_code"})

    def _server(self, repo: FakeSqlGateway, research: FakeSqlGateway) -> MCPServer:
        """A repo-docs server that also reads research, with the code-mode profile on."""
        return create_mcp_server(
            repo,
            _describe_handler(_schema()),
            search_handler=SearchHandler(repo, FakeEmbedding()),
            fetch_handler=FetchHandler(repo, "repo-docs"),
            graph_expand_handler=GraphExpandHandler(_no_reach(repo), "repo-docs"),
            find_paths_handler=GraphPathsHandler(repo, "repo-docs"),
            envelope_handler=EnvelopeHandler(repo, collection="repo-docs"),
            read_surfaces=(
                _routed_surface("repo-docs", repo, stamp=_fresh("repo-docs")),
                _routed_surface("research", research, stamp=_fresh("research")),
            ),
            only=self._CODE_MODE_ONLY,
            stamp=_fresh("repo-docs"),
            collection="repo-docs",
        )

    async def test_a_sandbox_hit_from_another_base_is_fetchable_and_stamped_by_that_base(self):
        repo, research = FakeSqlGateway(), _research_gateway()
        # `str(...)` because a hit's `ref` is optional on the contract, and the sandbox type-checks
        # the program before running it.
        code = (
            "found = await search(query='graph evidence', collection='research')\n"
            "ref = str(found['hits'][0]['ref'])\n"
            "unit = await fetch(unit_id=ref)\n"
            "{'ref': ref, 'searched': found['knowledge_base'], 'read': unit}\n"
        )

        async with Client(self._server(repo, research)) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert not result.is_error, _text(result)
        assert _text(result).startswith("{"), _text(result)
        payload = json.loads(_text(result))
        assert payload["ref"] == _RESEARCH_PAGE_REF
        assert payload["searched"]["slug"] == "research"
        assert payload["read"]["knowledge_base"]["slug"] == "research", "the base that answered the read is the base that stamped it"
        assert payload["read"]["ref"] == _RESEARCH_PAGE_REF
        assert "research findings" in str(payload["read"]["content"])
        assert repo.rows_calls == [], "the base the program did not name reads nothing"

    async def test_a_sandbox_collection_no_surface_serves_is_refused(self):
        repo, research = FakeSqlGateway(), _research_gateway()
        code = "await search(query='graph', collection='unregistered')\n"

        async with Client(self._server(repo, research)) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert "unregistered" in _text(result)
        assert "repo-docs" in _text(result)
        assert "research" in _text(result)
        assert research.rows_calls == []

    async def test_the_bound_functions_teach_the_registered_set_not_the_database(self):
        """A program reads the bound functions' own descriptions; they must not offer a collection this server cannot serve."""
        repo, research = FakeSqlGateway(), _research_gateway()
        toolset = _build_sandbox_toolset(
            RetrievalHandlers(
                search=SearchHandler(repo, FakeEmbedding()),
                fetch=FetchHandler(repo, "repo-docs"),
                graph_expand=GraphExpandHandler(_no_reach(repo), "repo-docs"),
                find_paths=GraphPathsHandler(repo, "repo-docs"),
            ),
            gateway=repo,
            embed_query=None,
            describe_schema=make_describe_schema(_describe_handler(_schema())),
            search_hybrid=False,
            collection="repo-docs",
            read_surfaces=(
                _routed_surface("repo-docs", repo, stamp=_fresh("repo-docs")),
                _routed_surface("research", research, stamp=_fresh("research")),
            ),
        )

        described = {name: tool.description or "" for name, tool in toolset.tools.items()}

        assert "one of: repo-docs, research" in described["search"]
        assert "`ref` to `fetch` exactly as returned" in described["search"]
        assert "api.about" not in described["search"], "a collection this server does not serve is not on offer"
        assert "already names its knowledge base" in described["fetch"]

    async def test_the_bound_graph_functions_teach_search_and_fetch_too(self):
        """The graph tools' own descriptions must already say `search` and `fetch` exist.

        `build_retrieval_toolset` registers ONLY `graph_expand`/`find_paths` here; `add_routed_read_tools`
        adds `search`/`fetch` onto the SAME toolset right after. A program sees all four bound
        together, not the narrower two-tool set this one internal call alone would suggest.
        """
        repo, research = FakeSqlGateway(), _research_gateway()
        toolset = _build_sandbox_toolset(
            RetrievalHandlers(
                search=SearchHandler(repo, FakeEmbedding()),
                fetch=FetchHandler(repo, "repo-docs"),
                graph_expand=GraphExpandHandler(_no_reach(repo), "repo-docs"),
                find_paths=GraphPathsHandler(repo, "repo-docs"),
            ),
            gateway=repo,
            embed_query=None,
            describe_schema=make_describe_schema(_describe_handler(_schema())),
            search_hybrid=False,
            collection="repo-docs",
            read_surfaces=(
                _routed_surface("repo-docs", repo, stamp=_fresh("repo-docs")),
                _routed_surface("research", research, stamp=_fresh("research")),
            ),
        )

        described = {name: tool.description or "" for name, tool in toolset.tools.items()}

        assert "`search`" in described["graph_expand"]
        assert "fetch(unit_id=" in described["graph_expand"]
        assert "`search`" in described["find_paths"]
        assert "fetch(unit_id=" in described["find_paths"]


def _graph_rows(count: int, *, title: str = "T") -> tuple[dict[str, object], ...]:
    """The seed's own row plus `count` reached-node rows, each with the edge that reached it."""
    seed = _node_cells("paper.graph", title=title) | _edge_cells(None, None, None)
    return (seed, *(
        _node_cells(f"ent.n{index:03d}", title=title) | _edge_cells("paper.graph", f"ent.n{index:03d}", "cites", f"paper.graph#c{index}")
        for index in range(count)
    ))


def _route_rows(count: int, *, title: str = "T") -> tuple[dict[str, object], ...]:
    """`count` two-node routes, one `path_rank` group each, in rank order."""
    rows: list[dict[str, object]] = []
    for rank in range(1, count + 1):
        rows.append({"path_rank": rank, "score": 0.5} | _node_cells("paper.graph", title=title) | _edge_cells(None, None, None))
        rows.append(
            {"path_rank": rank, "score": 0.5}
            | _node_cells(f"ent.n{rank:03d}", title=title)
            | _edge_cells("paper.graph", f"ent.n{rank:03d}", "cites", f"paper.graph#c{rank}")
        )
    return tuple(rows)


def _stamped_graph_server(gateway: FakeSqlGateway, *, only: frozenset[str] | None = None) -> MCPServer:
    """The graph surface with the freshness stamp of the knowledge base that serves it."""
    return create_mcp_server(
        FakeSqlGateway(),
        _describe_handler(_schema()),
        graph_expand_handler=GraphExpandHandler(_no_reach(gateway), "research"),
        find_paths_handler=GraphPathsHandler(gateway, "research"),
        collection=_COLLECTION,
        only=only,
        stamp=_fresh("research"),
    )


class TestGraphAnswersCarryTheRouteThatServedThem:
    async def test_an_expansion_carries_the_stamp_and_the_qualified_refs(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=_graph_rows(1), columns=()))
        server = _stamped_graph_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("graph_expand", {"seed_ids": ["paper.graph"]})

        payload = json.loads(_text(result))
        assert payload["knowledge_base"] == {"slug": "research", "indexed_at": None, "changed_since": 0}
        assert {node["ref"] for node in payload["nodes"]} == {"page:research/paper.graph", "page:research/ent.n000"}
        assert [edge["provenance_ref"] for edge in payload["edges"]] == ["unit:research/paper.graph#c0"]

    async def test_routes_carry_the_stamp_and_the_qualified_refs(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=_route_rows(1), columns=()))
        server = _stamped_graph_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("find_paths", {"from_id": "paper.graph", "to_id": "ent.n001"})

        payload = json.loads(_text(result))
        assert payload["knowledge_base"]["slug"] == "research"
        (route,) = payload["paths"]
        assert [node["ref"] for node in route["nodes"]] == ["page:research/paper.graph", "page:research/ent.n001"]
        assert [edge["provenance_ref"] for edge in route["edges"]] == ["unit:research/paper.graph#c1"]

    async def test_find_paths_answers_the_typed_result_not_an_untyped_paths_dict(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=_route_rows(1), columns=()))
        server = _stamped_graph_server(gateway)

        async with Client(server) as client:
            listing = await client.list_tools()
            result = await client.call_tool("find_paths", {"from_id": "paper.graph", "to_id": "ent.n001"})

        schema = next(tool.output_schema for tool in listing.tools if tool.name == "find_paths")
        assert schema is not None
        assert result.structured_content is not None
        assert set(result.structured_content) == {"paths", "next_steps", "knowledge_base", "window", "reach"}

    async def test_the_currency_opt_in_reaches_the_traversal(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        server = _stamped_graph_server(gateway)

        async with Client(server) as client:
            await client.call_tool("graph_expand", {"seed_ids": ["paper.graph"], "include_superseded": True})
            expansion = gateway.last.params
            await client.call_tool("find_paths", {"from_id": "a", "to_id": "b", "include_superseded": True})

        assert expansion["include_superseded"] is True
        assert gateway.last.params["include_superseded"] is True


class TestGraphNextStepsNameOnlyWhatTheProfileExposes:
    async def test_an_empty_expansion_on_the_full_profile_offers_search(self):
        server = _full_server(FakeSqlGateway(routed={"api.graph_expand": SqlResult(rows=(), columns=())}), FakeEmbedding(), stamp=_fresh("research"))

        async with Client(server) as client:
            result = await client.call_tool("graph_expand", {"seed_ids": ["paper.gone"]})

        options = " ".join(json.loads(_text(result))["next_steps"]["options"])
        assert "search(" in options
        assert "include_superseded" in options

    async def test_an_empty_expansion_on_a_graph_only_profile_never_offers_search(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        server = _stamped_graph_server(gateway, only=frozenset({"graph_expand", "find_paths"}))

        async with Client(server) as client:
            listing = await client.list_tools()
            result = await client.call_tool("graph_expand", {"seed_ids": ["paper.gone"]})

        assert "search" not in {tool.name for tool in listing.tools}
        steps = json.loads(_text(result))["next_steps"]
        assert "search(" not in " ".join(steps["options"])
        assert steps["summary"]

    async def test_an_expansion_on_a_profile_without_fetch_never_offers_fetch(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=_graph_rows(1), columns=()))
        server = _stamped_graph_server(gateway, only=frozenset({"graph_expand", "find_paths"}))

        async with Client(server) as client:
            result = await client.call_tool("graph_expand", {"seed_ids": ["paper.graph"]})

        assert "fetch(" not in " ".join(json.loads(_text(result))["next_steps"]["options"])


class TestGraphAnswersStayInsideTheirBudget:
    async def test_a_giant_neighbourhood_is_cut_to_fit_and_says_so(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=_graph_rows(40, title="T" * 400), columns=()))
        server = _stamped_graph_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("graph_expand", {"seed_ids": ["paper.graph"], "k": 50})

        payload = json.loads(_text(result))
        # The wire text is the indented rendering MCPServer writes. The budget is measured over the
        # model itself, which is what the contract caps.
        assert len(GraphResult.model_validate(payload).model_dump_json()) <= GRAPH_RESULT_CHARS
        assert payload["window"]["truncated"] is True
        assert payload["window"]["reason"]
        reached = {node["id"] for node in payload["nodes"]}
        assert all(edge["src_id"] in reached and edge["dst_id"] in reached for edge in payload["edges"])

    async def test_a_giant_route_set_is_cut_to_fit_and_says_so(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=_route_rows(50, title="T" * 400), columns=()))
        server = _stamped_graph_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("find_paths", {"from_id": "paper.graph", "to_id": "ent.n050", "k": 50})

        payload = json.loads(_text(result))
        assert len(GraphPathsResult.model_validate(payload).model_dump_json()) <= GRAPH_RESULT_CHARS
        assert payload["window"]["truncated"] is True
        assert all(len(route["edges"]) == 1 for route in payload["paths"])  # no route came back half-asserted

    async def test_a_multibyte_title_is_shortened_at_a_character_boundary_with_its_ref_intact(self):
        rows = (_node_cells("paper.graph", title="漢" * 20_000) | _edge_cells(None, None, None),)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        server = _stamped_graph_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("graph_expand", {"seed_ids": ["paper.graph"]})

        (node,) = json.loads(_text(result))["nodes"]
        assert node["ref"] == "page:research/paper.graph"
        assert node["title"].endswith("…")
        assert set(node["title"][:-1]) == {"漢"}


class TestAGraphRefusalStaysARefusal:
    """A surface adds a stamp and a next move to a refusal; it never talks it back into an empty answer."""

    async def test_a_giant_node_identity_refuses_and_never_echoes_the_identity(self):
        giant = "x" * 20_000
        rows = (_node_cells(giant) | _edge_cells(None, None, None),)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        server = _stamped_graph_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("graph_expand", {"seed_ids": [giant]})

        payload = json.loads(_text(result))
        assert payload["nodes"] == []
        assert payload["window"]["refused"] is True
        assert payload["window"]["reason"]
        assert payload["next_steps"]["options"]
        assert "No current node was reached" not in payload["next_steps"]["summary"]
        assert giant not in _text(result)
        assert payload["knowledge_base"]["slug"] == "research"

    async def test_a_giant_route_identity_refuses_on_the_paths_surface_too(self):
        giant = "y" * 20_000
        rows = ({"path_rank": 1, "score": 0.5} | _node_cells(giant) | _edge_cells(None, None, None),)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        server = _stamped_graph_server(gateway)

        async with Client(server) as client:
            result = await client.call_tool("find_paths", {"from_id": giant, "to_id": "ent.b"})

        payload = json.loads(_text(result))
        assert payload["paths"] == []
        assert payload["window"]["refused"] is True
        assert "No recorded route" not in payload["next_steps"]["summary"]
        assert giant not in _text(result)
