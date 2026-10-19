r"""Unit specs for the single-source retrieval toolset (`src/engram/retrieval/tools.py`).

engram's four retrieval tool functions are defined **once** (the core
`_search` / `_fetch` / `_graph_expand` / `_find_paths` helpers wrapping the CQRS
handlers) and registered into **both** a pydantic-ai :class:`~pydantic_ai.FunctionToolset`
(the eval agent-under-test) and a :class:`~mcp.server.fastmcp.FastMCP` server (the real MCP
surface) via thin adapters. These specs pin that single-source contract without a database:

* the core helpers project the handlers' :class:`SqlResult` / graph results into the exact
  `engram.retrieval.domain` result shapes (`list[SearchHit]` / `FetchResult` / `GraphResult` /
  `GraphPathsResult`) and thread the tool args through to the handler messages;
* :func:`build_retrieval_toolset` yields a :class:`FunctionToolset` exposing the four tools
  under their canonical names, each delegating to its core helper (asserted by calling the
  tools through the toolset and checking the handlers were driven with the right message);
* :func:`register_mcp_tools` registers the **same** four tools on a FastMCP server (asserted
  by listing the server's tools over the SDK's in-memory client session and calling them).

Fakes over mocks: :class:`FakeRetrievalHandlers` bundles four scriptable handler stubs that
record the message they were called with and return canned domain results, so a test asserts
both the projection (shape) and the routing (which handler, which args) in one place.
"""

from __future__ import annotations

import inspect
import json

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from core.ai.retrieval.contracts import GraphPathsResult, KnowledgeBaseStamp, ResultWindow, StampSource
from core.ai.retrieval.strategy import resolve_strategy
from pydantic_ai import ModelRetry

from engram.retrieval.domain import (
    Fetch,
    FetchResult,
    GraphExpand,
    GraphPath,
    GraphPaths,
    GraphResult,
    NextSteps,
    Search,
    SearchHit,
    SearchResult,
    SqlResult,
)
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.queries import GraphExpandHandler, GraphPathsHandler
from engram.retrieval.tools import (
    DATE_RANGE_CLAUSE,
    SEARCH_DESCRIPTION,
    SEARCH_DESCRIPTION_HYBRID,
    SEARCH_DESCRIPTION_KEYWORD,
    SEARCH_RESULT_CHARS,
    TOOL_MAX_RETRIES,
    TOOL_NAMES,
    RetrievalHandlers,
    bound_search_result,
    build_retrieval_toolset,
    elide_k,
    search_description,
)
from engram.testing.fakes.gateway import FakeSqlGateway

if TYPE_CHECKING:
    from mcp.types import CallToolResult
    from pydantic_ai.tools import RunContext
    from pydantic_ai.toolsets import AbstractToolset


_COLLECTION = "workdocs"
"""The made-up collection every door in this module is built over: there is no default one."""


# -- scriptable fakes for the four handlers ----------------------------------


@dataclass
class FakeSearchHandler:
    """Records the :class:`Search` it was called with; returns the scripted :class:`SqlResult`."""

    result: SqlResult = field(default_factory=SqlResult)
    calls: list[Search] = field(default_factory=list)

    async def __call__(self, query: Search) -> SqlResult:
        self.calls.append(query)
        return self.result


@dataclass
class FakeFetchHandler:
    """Records the :class:`Fetch` it was called with; returns the scripted :class:`SqlResult`."""

    result: SqlResult = field(default_factory=SqlResult)
    calls: list[Fetch] = field(default_factory=list)

    async def __call__(self, query: Fetch) -> SqlResult:
        self.calls.append(query)
        return self.result


@dataclass
class FakeGraphExpandHandler:
    """Records the :class:`GraphExpand` it was called with; returns the scripted :class:`GraphResult`."""

    result: GraphResult = field(default_factory=GraphResult)
    calls: list[GraphExpand] = field(default_factory=list)
    served: list[frozenset[str]] = field(default_factory=list)

    async def __call__(self, query: GraphExpand, *, tools: frozenset[str] = TOOL_NAMES) -> GraphResult:
        self.calls.append(query)
        self.served.append(tools)
        return self.result


@dataclass
class FakeGraphPathsHandler:
    """Records the :class:`GraphPaths` it was called with; returns the scripted routes as the typed result."""

    result: tuple[GraphPath, ...] = ()
    calls: list[GraphPaths] = field(default_factory=list)
    served: list[frozenset[str]] = field(default_factory=list)

    async def __call__(self, query: GraphPaths, *, tools: frozenset[str] = TOOL_NAMES) -> GraphPathsResult:
        self.calls.append(query)
        self.served.append(tools)
        return GraphPathsResult(
            paths=self.result,
            next_steps=NextSteps(summary=f"{len(self.result)} route(s)."),
            window=ResultWindow(returned_items=len(self.result)),
        )


def _handlers(
    *,
    search: FakeSearchHandler | None = None,
    fetch: FakeFetchHandler | None = None,
    graph_expand: FakeGraphExpandHandler | None = None,
    find_paths: FakeGraphPathsHandler | None = None,
) -> RetrievalHandlers:
    """A :class:`RetrievalHandlers` bundle of the four scriptable fakes (defaults where unset)."""
    return RetrievalHandlers(
        search=search or FakeSearchHandler(),
        fetch=fetch or FakeFetchHandler(),
        graph_expand=graph_expand or FakeGraphExpandHandler(),
        find_paths=find_paths or FakeGraphPathsHandler(),
    )


def _search_rows() -> SqlResult:
    """Two ranked ``api.search`` rows in the :class:`SearchHit` column shape."""
    return SqlResult(
        rows=(
            {
                "id": "paper.lightrag-2025#s2",
                "hit_level": "section",
                "page_id": "paper.lightrag-2025",
                "page_title": "LightRAG",
                "snippet": "dual-level retrieval",
                "score": 0.91,
            },
            {
                "id": "paper.graphrag-2024",
                "hit_level": "card",
                "page_id": "paper.graphrag-2024",
                "page_title": "GraphRAG",
                "snippet": None,
                "score": 0.42,
            },
        ),
        columns=("id", "hit_level", "page_id", "page_title", "snippet", "score"),
    )


def _envelope(collection: str = _COLLECTION) -> EnvelopeHandler:
    """A leveled read over an empty store, so `fetch` serves a map and `search` may offer it."""
    return EnvelopeHandler(FakeSqlGateway(), collection=collection)


def _fetch_row() -> SqlResult:
    """One ``api.fetch`` row in the :class:`FetchResult` column shape."""
    return SqlResult(
        rows=({"id": "paper.lightrag-2025", "level": "page", "title": "LightRAG", "body": "Full page body."},),
        columns=("id", "level", "title", "body"),
    )


def _ctx() -> RunContext[None]:
    """A real (minimal) pydantic-ai :class:`RunContext` for driving a toolset directly."""
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.tools import RunContext
    from pydantic_ai.usage import RunUsage

    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def _call(
    toolset: AbstractToolset[None], name: str, args: dict[str, object], *, tool_from: AbstractToolset[None] | None = None
) -> object:
    """Invoke a toolset tool by name with `args` through pydantic-ai's runtime, returning its result.

    Resolves the tool from the toolset against a real (minimal) :class:`RunContext` and calls
    it the way the agent's tool-execution loop would — this exercises the registered adapter
    end-to-end (pydantic arg validation + the core-helper projection), not a hand-rolled call.
    Typed against the `AbstractToolset` interface (just `get_tools` / `call_tool`), so it accepts
    both a bare `FunctionToolset` and a `RetrievalBudgetToolset` wrapper.

    `tool_from` resolves the tool definition from a different toolset than the one called —
    the over-budget stub tests use it to model the real mid-step sequence, where the model
    received the tool list BEFORE its call spent the last budget unit (the budget wrapper's
    `get_tools` stops offering query tools once the budget is spent, so resolving from the
    wrapper itself would `KeyError` before the stub path could ever run).
    """
    ctx = _ctx()
    tools = await (tool_from or toolset).get_tools(ctx)
    return await toolset.call_tool(name, args, ctx, tools[name])


# -- core helper projection (single source) ----------------------------------


class TestBuildToolsetRegistration:
    async def test_registers_the_four_retrieval_tools_under_canonical_names(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION)

        assert set(toolset.tools) == {"search", "fetch", "graph_expand", "find_paths"}

    async def test_every_tool_has_a_non_empty_description_for_the_agent(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION)

        descriptions = {name: (tool.description or "") for name, tool in toolset.tools.items()}
        assert all(descriptions.values())


class TestBuildToolsetRetryBudget:
    """Every structured tool carries a raised retry budget so a self-correctable ModelRetry survives.

    pydantic-ai's default per-tool `max_retries` is 1, so the SECOND `ModelRetry` in a run (e.g. a
    second hallucinated `fetch` id, whose "re-ground with search" hint is a self-correctable retry)
    would raise `UnexpectedModelBehavior: Tool 'fetch' exceeded max retries count of 1` and kill the
    whole run. This is the shared single source for MCP, evals and ask, so the budget is set once.
    """

    async def test_all_four_tools_carry_the_shared_retry_budget(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION)

        assert {name: tool.max_retries for name, tool in toolset.tools.items()} == {
            "search": TOOL_MAX_RETRIES,
            "fetch": TOOL_MAX_RETRIES,
            "graph_expand": TOOL_MAX_RETRIES,
            "find_paths": TOOL_MAX_RETRIES,
        }

    async def test_the_budget_is_above_the_pydantic_ai_default_of_one(self):
        # The default of 1 is exactly what crashed the run on the second bad id. The budget must be
        # more than one retry so the "re-ground with search" hint gets a chance to land.
        assert TOOL_MAX_RETRIES > 1

    async def test_forced_hybrid_search_also_carries_the_budget(self):
        # The search tool is registered by a separate helper per forced mode — it must not be the one
        # tool left at the fragile default.
        toolset = build_retrieval_toolset(_handlers(), search_hybrid=True, collection=_COLLECTION)

        assert toolset.tools["search"].max_retries == TOOL_MAX_RETRIES

    async def test_fetch_raises_model_retry_on_an_unknown_id(self):
        # The exact self-correctable failure the budget protects: an unknown/hallucinated id returns no
        # row, so the fetch tool raises ModelRetry telling the agent to re-ground with search. With the
        # raised budget a second such id retries again instead of crashing the run.
        toolset = build_retrieval_toolset(_handlers(fetch=FakeFetchHandler(result=SqlResult())), collection=_COLLECTION)

        with pytest.raises(ModelRetry) as exc:
            await _call(toolset, "fetch", {"unit_id": "paper.hallucinated-2099"})
        assert "search" in str(exc.value).lower()  # the actionable re-ground hint


class TestBuildToolsetSubsetAndMode:
    """The retrieval-strategy seam: `only` selects the tool subset; `search_hybrid` forces the leg."""

    async def test_only_registers_the_named_subset(self):
        toolset = build_retrieval_toolset(_handlers(), only=frozenset({"graph_expand", "find_paths"}), collection=_COLLECTION)

        assert set(toolset.tools) == {"graph_expand", "find_paths"}

    async def test_search_subset_drops_the_graph_tools(self):
        toolset = build_retrieval_toolset(_handlers(), only=frozenset({"search", "fetch"}), search_hybrid=True, collection=_COLLECTION)

        assert set(toolset.tools) == {"search", "fetch"}

    async def test_default_search_keeps_the_agent_controlled_bm25_flag(self):
        # `search_hybrid=None` (the default) keeps the legacy agent-controlled search with `bm25_only`.
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        await _call(toolset, "search", {"query": "graph rag", "bm25_only": True})

        (message,) = search.calls
        assert message.hybrid is False  # the agent chose BM25-only via the arg

    async def test_forced_hybrid_search_runs_hybrid_with_no_bm25_arg(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), search_hybrid=True, collection=_COLLECTION)

        # The forced-mode search tool drops the `bm25_only` arg — it is not part of the signature.
        assert "bm25_only" not in inspect.signature(toolset.tools["search"].function).parameters
        await _call(toolset, "search", {"query": "graph rag"})

        (message,) = search.calls
        assert message.hybrid is True

    async def test_forced_keyword_search_runs_bm25_only(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), search_hybrid=False, collection=_COLLECTION)

        await _call(toolset, "search", {"query": "graph rag"})

        (message,) = search.calls
        assert message.hybrid is False


class TestBuildToolsetDescriptionOverride:
    """The per-tool description override seam: `descriptions` replaces a tool's shipped default."""

    async def test_overrides_only_the_named_tools_others_keep_defaults(self):
        toolset = build_retrieval_toolset(_handlers(), descriptions={"search": "X", "fetch": "Y"}, collection=_COLLECTION)

        tools = toolset.tools
        assert tools["search"].description == "X"
        assert tools["fetch"].description == "Y"
        # The unnamed tools keep their shipped defaults (non-empty, not the overrides).
        assert tools["graph_expand"].description not in {"", "X", "Y"}
        assert tools["find_paths"].description not in {"", "X", "Y"}

    async def test_none_keeps_every_shipped_default(self):
        defaults = build_retrieval_toolset(_handlers(), collection=_COLLECTION)
        overridden = build_retrieval_toolset(_handlers(), descriptions=None, collection=_COLLECTION)

        assert {name: tool.description for name, tool in overridden.tools.items()} == {
            name: tool.description for name, tool in defaults.tools.items()
        }

    async def test_overrides_forced_hybrid_search(self):
        toolset = build_retrieval_toolset(_handlers(), search_hybrid=True, descriptions={"search": "FORCED"}, collection=_COLLECTION)

        assert toolset.tools["search"].description == "FORCED"

    async def test_overrides_forced_keyword_search(self):
        toolset = build_retrieval_toolset(_handlers(), search_hybrid=False, descriptions={"search": "FORCED"}, collection=_COLLECTION)

        assert toolset.tools["search"].description == "FORCED"

    async def test_unknown_keys_are_ignored(self):
        # An override map may carry entries for tools outside the selected `only` subset — harmless.
        toolset = build_retrieval_toolset(
            _handlers(), only=frozenset({"fetch"}), descriptions={"search": "X", "unknown": "Z"}, collection=_COLLECTION
        )

        assert set(toolset.tools) == {"fetch"}
        assert toolset.tools["fetch"].description not in {"", "X", "Z"}  # fetch keeps its default


class TestSearchTool:
    async def test_threads_the_args_into_a_search_message(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        await _call(toolset, "search", {"query": "graph rag", "k": 5, "bm25_only": True})

        (message,) = search.calls
        assert isinstance(message, Search)
        assert message.query == "graph rag"
        assert message.k == 5
        assert message.facets == {}  # the agent tool exposes no facets: a facet pre-filter an LLM guesses wrong zeroes retrieval
        assert message.hybrid is False  # bm25_only=True → hybrid disabled

    async def test_defaults_match_the_spec(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        await _call(toolset, "search", {"query": "graph rag"})

        (message,) = search.calls
        assert message.k == 12
        assert message.facets == {}
        assert message.hybrid is True  # bm25_only defaults False → hybrid

    async def test_projects_rows_into_search_hits(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        result = await _call(toolset, "search", {"query": "graph rag"})

        assert isinstance(result, SearchResult)
        hits = result.hits
        first = hits[0]
        assert isinstance(first, SearchHit)
        assert [hit.id for hit in hits if isinstance(hit, SearchHit)] == ["paper.lightrag-2025#s2", "paper.graphrag-2024"]
        assert first.hit_level == "section"
        assert first.score == 0.91
        assert first.answered_by == "hybrid"  # the same leg the search ran, threaded onto every hit
        assert first.ref == "section:workdocs/paper.lightrag-2025#s2"
        assert hits[1].ref == "page:workdocs/paper.graphrag-2024"


class TestSearchCollectionOverride:
    """`search` takes an optional `collection`, which on a single-source door names only its own partition."""

    async def test_omitted_collection_keeps_the_doors_own_default(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection="workdocs")

        await _call(toolset, "search", {"query": "graph rag"})

        (message,) = search.calls
        assert message.collection == "workdocs"

    async def test_another_collection_is_refused_before_the_search_runs(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection="workdocs")

        with pytest.raises(ModelRetry, match="'workdocs'"):
            await _call(toolset, "search", {"query": "graph rag", "collection": "other-knowledge-base"})

        assert search.calls == []

    async def test_the_doors_own_collection_given_by_name_is_searched(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection="workdocs")

        result = await _call(toolset, "search", {"query": "graph rag", "collection": "workdocs"})

        assert isinstance(result, SearchResult)
        assert {hit.knowledge_base for hit in result.hits} == {"workdocs"}


_PERIOD_ARGUMENT = {"anyOf": [{"pattern": r"^\d{4}(-\d{2}(-\d{2})?)?$", "type": "string"}, {"type": "null"}], "default": None}
_SEARCH_ARGUMENTS: dict[str, object] = {
    "query": {"type": "string"},
    "k": {"default": 12, "type": "integer"},
    "bm25_only": {"default": False, "type": "boolean"},
    "collection": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
    "since": _PERIOD_ARGUMENT,
    "until": _PERIOD_ARGUMENT,
    "facets": {
        "anyOf": [{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "type": "object"}, {"type": "null"}],
        "default": None,
    },
}
"""Each `search` argument's JSON schema, exactly as the unpinned tool publishes it."""


def _without_facets(description: str) -> str:
    """A shipped search description as a knowledge base declaring no facets publishes it."""
    return description.replace(DATE_RANGE_CLAUSE, f"{DATE_RANGE_CLAUSE} This knowledge base declares no facets, so leave `facets` out.")


_SEARCH_VARIANTS = [
    pytest.param(
        None, False, ("query", "k", "bm25_only", "collection", "since", "until", "facets"), _without_facets(SEARCH_DESCRIPTION), id="agent-choice"
    ),
    pytest.param(
        None,
        True,
        ("query", "bm25_only", "collection", "since", "until", "facets"),
        elide_k(_without_facets(SEARCH_DESCRIPTION)),
        id="agent-choice-capped",
    ),
    pytest.param(
        True, False, ("query", "k", "collection", "since", "until", "facets"), _without_facets(SEARCH_DESCRIPTION_HYBRID), id="forced-hybrid"
    ),
    pytest.param(
        True,
        True,
        ("query", "collection", "since", "until", "facets"),
        elide_k(_without_facets(SEARCH_DESCRIPTION_HYBRID)),
        id="forced-hybrid-capped",
    ),
    pytest.param(
        False, False, ("query", "k", "collection", "since", "until", "facets"), _without_facets(SEARCH_DESCRIPTION_KEYWORD), id="forced-keyword"
    ),
    pytest.param(
        False,
        True,
        ("query", "collection", "since", "until", "facets"),
        elide_k(_without_facets(SEARCH_DESCRIPTION_KEYWORD)),
        id="forced-keyword-capped",
    ),
]
"""Every unpinned `search` closure as (search_hybrid, capped, its arguments in order, its description over a SQL-capable surface)."""

_PINNED_VARIANTS = [
    pytest.param(None, False, ("query", "k", "bm25_only", "since", "until", "facets"), id="agent-choice"),
    pytest.param(None, True, ("query", "bm25_only", "since", "until", "facets"), id="agent-choice-capped"),
    pytest.param(True, False, ("query", "k", "since", "until", "facets"), id="forced-hybrid"),
    pytest.param(True, True, ("query", "since", "until", "facets"), id="forced-hybrid-capped"),
    pytest.param(False, False, ("query", "k", "since", "until", "facets"), id="forced-keyword"),
    pytest.param(False, True, ("query", "since", "until", "facets"), id="forced-keyword-capped"),
]
"""Every pinned `search` closure as (search_hybrid, capped, the arguments it still offers in order)."""

_SQL_SENTENCE_TAIL = ", and `api.about` (via `run_sql`) lists what is here and what each one covers."
"""How the collection sentence ends over a surface that also serves `run_sql`. Without `run_sql` it ends at the default."""

_SHARED_SENTENCE = (
    "This database is shared across more than one collection. Set `collection` to search a different one instead of this "
    "knowledge base's own default"
)
"""The shipped collection sentence up to its ending, which a single-source door replaces."""


def _served_by_one(description: str) -> str:
    """A shipped search description as a single-source door publishes it: its collection sentence names the one it reads."""
    served = f"This tool searches one collection, '{_COLLECTION}': leave `collection` out, since any other value is refused."
    return description.replace(f"{_SHARED_SENTENCE}{_SQL_SENTENCE_TAIL}", served).replace(f"{_SHARED_SENTENCE}.", served)


def _search_schema(arguments: tuple[str, ...]) -> dict[str, object]:
    """The `search` parameter schema publishing exactly `arguments`, in their order."""
    return {"additionalProperties": False, "properties": {name: _SEARCH_ARGUMENTS[name] for name in arguments}, "required": ["query"], "type": "object"}


def _raw_row() -> SqlResult:
    """One `api.search` row for a raw source chunk."""
    return SqlResult(
        rows=(
            {
                "id": "source.brine-notes#c3",
                "hit_level": "raw",
                "page_id": "source.brine-notes",
                "page_title": "Brine notes",
                "snippet": "salt to water by weight",
                "score": 0.5,
            },
        ),
        columns=("id", "hit_level", "page_id", "page_title", "snippet", "score"),
    )


class TestPinnedSearch:
    """`pinned=True` holds `search` to the door's own collection, which the model is never offered, and keeps the date range."""

    @pytest.mark.parametrize(("search_hybrid", "capped", "arguments"), _PINNED_VARIANTS)
    @pytest.mark.parametrize("can_sql", [True, False])
    async def test_the_pinned_schema_offers_the_date_range_and_no_collection(
        self, search_hybrid: bool | None, capped: bool, arguments: tuple[str, ...], can_sql: bool
    ):
        toolset = build_retrieval_toolset(
            _handlers(), search_hybrid=search_hybrid, k_source=(lambda: 7) if capped else None, collection=_COLLECTION, can_sql=can_sql, pinned=True
        )

        tools = await toolset.get_tools(_ctx())

        assert tools["search"].tool_def.parameters_json_schema == _search_schema(arguments)

    @pytest.mark.parametrize("search_hybrid", [None, True, False])
    @pytest.mark.parametrize("capped", [False, True])
    @pytest.mark.parametrize("can_sql", [True, False])
    async def test_the_pinned_description_keeps_the_date_sentences_and_drops_the_collection_sentences(
        self, search_hybrid: bool | None, capped: bool, can_sql: bool
    ):
        toolset = build_retrieval_toolset(
            _handlers(), search_hybrid=search_hybrid, k_source=(lambda: 7) if capped else None, collection=_COLLECTION, can_sql=can_sql, pinned=True
        )

        tools = await toolset.get_tools(_ctx())

        text = tools["search"].tool_def.description or ""
        assert DATE_RANGE_CLAUSE in text
        assert "`collection`" not in text
        assert "shared across more than one collection" not in text
        assert "  " not in text
        assert "Start here" in text

    @pytest.mark.parametrize("override", [SEARCH_DESCRIPTION, search_description(hybrid=True, can_sql=False)], ids=["with-sql", "without-sql"])
    async def test_a_pinned_search_strips_only_the_collection_sentences_from_an_override(self, override: str):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION, descriptions={"search": override}, pinned=True)

        tools = await toolset.get_tools(_ctx())

        text = tools["search"].tool_def.description or ""
        assert DATE_RANGE_CLAUSE in override
        assert DATE_RANGE_CLAUSE in text
        assert "shared across more than one collection" not in text
        assert "`collection`" not in text
        assert "`api.about`" not in text
        assert "  " not in text
        assert "Start here" in text

    @pytest.mark.parametrize(("search_hybrid", "capped", "arguments", "description"), _SEARCH_VARIANTS)
    @pytest.mark.parametrize("can_sql", [True, False])
    async def test_an_unpinned_search_publishes_todays_schema_and_description(
        self, search_hybrid: bool | None, capped: bool, arguments: tuple[str, ...], description: str, can_sql: bool
    ):
        toolset = build_retrieval_toolset(
            _handlers(), search_hybrid=search_hybrid, k_source=(lambda: 7) if capped else None, collection=_COLLECTION, can_sql=can_sql, pinned=False
        )

        tools = await toolset.get_tools(_ctx())

        published = tools["search"].tool_def
        expected = _served_by_one(description if can_sql else description.replace(_SQL_SENTENCE_TAIL, "."))
        assert _SQL_SENTENCE_TAIL in description
        assert _SHARED_SENTENCE not in expected
        assert toolset.tools["search"].prepare is None
        assert json.dumps(published.parameters_json_schema) == json.dumps(_search_schema(arguments))
        assert published.description == expected

    async def test_a_pinned_search_searches_its_own_collection_whatever_the_model_passes(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION, pinned=True)

        result = await _call(toolset, "search", {"query": "graph rag", "collection": "other"})

        (message,) = search.calls
        assert message.collection == _COLLECTION
        assert isinstance(result, SearchResult)
        assert {hit.knowledge_base for hit in result.hits} == {_COLLECTION}

    async def test_a_pinned_search_keeps_a_raw_chunk(self):
        search = FakeSearchHandler(result=_raw_row())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION, pinned=True)

        result = await _call(toolset, "search", {"query": "brine ratio"})

        assert isinstance(result, SearchResult)
        assert [(hit.hit_level, hit.ref) for hit in result.hits] == [("raw", "unit:workdocs/source.brine-notes#c3")]
        (message,) = search.calls
        assert (message.since, message.until) == (None, None)


class TestSearchNextSteps:
    """Every `search` result carries a `next_steps` block: what to do with the hits it just returned."""

    async def test_hits_point_at_fetching_the_top_one(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        result = await _call(toolset, "search", {"query": "graph rag"})

        assert isinstance(result, SearchResult)
        assert "2 hit" in result.next_steps.summary
        (option,) = result.next_steps.options
        assert "section:workdocs/paper.lightrag-2025#s2" in option
        assert "fetch" in option
        assert "never the source" in option  # a hit is an address plus a snippet, not the text itself

    async def test_no_hits_names_the_map_as_the_way_out(self):
        toolset = build_retrieval_toolset(_handlers(search=FakeSearchHandler(result=SqlResult())), collection=_COLLECTION, envelope=_envelope())

        result = await _call(toolset, "search", {"query": "no such thing anywhere"})

        assert isinstance(result, SearchResult)
        assert result.hits == ()
        assert "No hits" in result.next_steps.summary
        joined = " ".join(result.next_steps.options)
        assert f"fetch(unit_id='collection:{_COLLECTION}')" in joined  # the door's own collection
        assert "map" in joined

    async def test_a_scoped_collection_names_its_own_map(self):
        toolset = build_retrieval_toolset(_handlers(search=FakeSearchHandler(result=SqlResult())), collection="workdocs", envelope=_envelope("workdocs"))

        result = await _call(toolset, "search", {"query": "no such thing anywhere"})

        assert isinstance(result, SearchResult)
        joined = " ".join(result.next_steps.options)
        assert "fetch(unit_id='collection:workdocs')" in joined


class TestSearchHitsCarryTheirOwnCollection:
    """A single-knowledge-base door's hits are stamped `collection`.

    The same string `search` would take as `knowledge_base` for this base were it mounted on the library
    door instead.
    """

    async def test_a_scoped_collection_stamps_every_hit(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection="workdocs")

        result = await _call(toolset, "search", {"query": "graph rag"})

        assert isinstance(result, SearchResult)
        assert {hit.knowledge_base for hit in result.hits} == {"workdocs"}

    async def test_the_summary_says_hybrid_answered(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        result = await _call(toolset, "search", {"query": "graph rag"})

        assert isinstance(result, SearchResult)
        assert result.next_steps.summary.endswith("Answered by hybrid.")

    async def test_the_summary_says_lexical_answered(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        result = await _call(toolset, "search", {"query": "graph rag", "bm25_only": True})

        assert isinstance(result, SearchResult)
        assert result.next_steps.summary.endswith("Answered by lexical.")

    async def test_the_no_hits_summary_also_says_what_answered(self):
        toolset = build_retrieval_toolset(_handlers(search=FakeSearchHandler(result=SqlResult())), collection=_COLLECTION)

        result = await _call(toolset, "search", {"query": "no such thing anywhere", "bm25_only": True})

        assert isinstance(result, SearchResult)
        assert result.next_steps.summary.endswith("Answered by lexical.")


class TestFetchTool:
    async def test_threads_the_unit_id_into_a_fetch_message(self):
        fetch = FakeFetchHandler(result=_fetch_row())
        toolset = build_retrieval_toolset(_handlers(fetch=fetch), collection=_COLLECTION)

        await _call(toolset, "fetch", {"unit_id": "paper.lightrag-2025"})

        (message,) = fetch.calls
        assert isinstance(message, Fetch)
        assert message.id == "paper.lightrag-2025"

    async def test_projects_the_first_row_into_a_fetch_result(self):
        fetch = FakeFetchHandler(result=_fetch_row())
        toolset = build_retrieval_toolset(_handlers(fetch=fetch), collection=_COLLECTION)

        unit = await _call(toolset, "fetch", {"unit_id": "paper.lightrag-2025"})

        assert isinstance(unit, FetchResult)
        assert unit.id == "paper.lightrag-2025"
        assert unit.level == "page"
        assert unit.title == "LightRAG"
        assert unit.body == "Full page body."


def _stamp_source(stamp: KnowledgeBaseStamp) -> tuple[StampSource, list[int]]:
    """A `StampSource` returning `stamp` on every call, recording one entry per call in `calls`."""
    calls: list[int] = []

    async def source() -> KnowledgeBaseStamp:
        calls.append(1)
        return stamp

    return source, calls


class TestTheStampRidesEveryAnswer:
    async def test_a_search_answer_carries_the_stamp(self):
        stamp = KnowledgeBaseStamp(slug="repo-docs", changed_since=2)
        source, calls = _stamp_source(stamp)
        toolset = build_retrieval_toolset(_handlers(search=FakeSearchHandler(result=_search_rows())), stamp=source, collection=_COLLECTION)

        result = await _call(toolset, "search", {"query": "graph"})

        assert isinstance(result, SearchResult)
        assert result.knowledge_base == stamp
        assert calls == [1]

    async def test_a_flat_fetch_answer_carries_the_stamp(self):
        stamp = KnowledgeBaseStamp(slug="repo-docs")
        source, _ = _stamp_source(stamp)
        toolset = build_retrieval_toolset(_handlers(fetch=FakeFetchHandler(result=_fetch_row())), stamp=source, collection=_COLLECTION)

        result = await _call(toolset, "fetch", {"unit_id": "paper.lightrag-2025"})

        assert isinstance(result, FetchResult)
        assert result.knowledge_base == stamp

    async def test_the_stamp_is_read_once_per_answer_not_once_per_hit(self):
        source, calls = _stamp_source(KnowledgeBaseStamp(slug="repo-docs"))
        toolset = build_retrieval_toolset(_handlers(search=FakeSearchHandler(result=_search_rows())), stamp=source, collection=_COLLECTION)

        await _call(toolset, "search", {"query": "graph"})

        assert calls == [1]


class TestGraphExpandTool:
    async def test_threads_the_args_into_a_graph_expand_message(self):
        graph_expand = FakeGraphExpandHandler()
        toolset = build_retrieval_toolset(_handlers(graph_expand=graph_expand), collection=_COLLECTION)

        await _call(toolset, "graph_expand", {"seed_ids": ["paper.graph", "ent.bm25"], "hops": 3})

        (message,) = graph_expand.calls
        assert isinstance(message, GraphExpand)
        assert message.seed_ids == ("paper.graph", "ent.bm25")
        assert message.hops == 3

    async def test_returns_the_graph_result_unchanged(self):
        result = GraphResult()
        toolset = build_retrieval_toolset(_handlers(graph_expand=FakeGraphExpandHandler(result=result)), collection=_COLLECTION)

        out = await _call(toolset, "graph_expand", {"seed_ids": ["paper.graph"]})

        assert isinstance(out, GraphResult)


class TestFindPathsTool:
    async def test_threads_the_endpoints_into_a_graph_paths_message(self):
        find_paths = FakeGraphPathsHandler()
        toolset = build_retrieval_toolset(_handlers(find_paths=find_paths), collection=_COLLECTION)

        await _call(toolset, "find_paths", {"from_id": "paper.graph", "to_id": "ent.bm25"})

        (message,) = find_paths.calls
        assert isinstance(message, GraphPaths)
        assert message.from_id == "paper.graph"
        assert message.to_id == "ent.bm25"

    async def test_returns_a_graph_paths_result_carrying_the_same_paths_in_order(self):
        first, second = GraphPath(nodes=(), edges=(), score=0.9), GraphPath(nodes=(), edges=(), score=0.4)
        toolset = build_retrieval_toolset(_handlers(find_paths=FakeGraphPathsHandler(result=(first, second))), collection=_COLLECTION)

        out = await _call(toolset, "find_paths", {"from_id": "a", "to_id": "b"})

        assert isinstance(out, GraphPathsResult)
        assert out.paths == (first, second)
        assert out.window == ResultWindow(returned_items=len(out.paths))
        assert out.next_steps.summary

def _text(result: CallToolResult) -> str:
    """The first text block of an MCP tool result (the tools return a single text payload)."""
    block = result.content[0]
    assert block.type == "text"
    return block.text


# -- component support, bounded snippets, and the response budget -------------


def _ranked_row(
    unit_id: str,
    *,
    lexical_rank: int | None,
    vector_rank: int | None,
    score: float = 0.5,
    snippet: str | None = "an excerpt",
    page_title: str | None = "A Page",
    hit_level: str = "section",
) -> dict[str, object]:
    """One `api.search` row carrying the per-leg facts the new grammar projects."""
    return {
        "id": unit_id,
        "hit_level": hit_level,
        "page_id": unit_id.partition("#")[0],
        "page_title": page_title,
        "snippet": snippet,
        "score": score,
        "lexical_rank": lexical_rank,
        "vector_rank": vector_rank,
        "vector_distance": None if vector_rank is None else 0.31,
        "snippet_truncated": False,
    }


_RANKED_COLUMNS = (
    "id",
    "hit_level",
    "page_id",
    "page_title",
    "snippet",
    "score",
    "lexical_rank",
    "vector_rank",
    "vector_distance",
    "snippet_truncated",
)


async def _search_result(
    rows: tuple[dict[str, object], ...], *, collection: str = _COLLECTION, query: str = "graph rag", **args: object
) -> SearchResult:
    """Drive the registered `search` tool of a leveled toolset over scripted rows and return its result."""
    handlers = _handlers(search=FakeSearchHandler(result=SqlResult(rows=rows, columns=_RANKED_COLUMNS)))
    toolset = build_retrieval_toolset(handlers, collection=collection, envelope=_envelope(collection))
    result = await _call(toolset, "search", {"query": query, **args})
    assert isinstance(result, SearchResult)
    return result


class TestComponentSupport:
    """A hit says which legs selected it; a result says what the search as a whole found."""

    async def test_both_legs_make_a_hit_lexical_and_semantic(self):
        result = await _search_result((_ranked_row("p.one#s1", lexical_rank=1, vector_rank=2),))

        (hit,) = result.hits
        assert hit.lexical_rank == 1
        assert hit.vector_rank == 2
        assert hit.support == "lexical_and_semantic"
        assert hit.answered_by == "hybrid"
        assert result.status == "lexically_supported_candidates"

    async def test_a_missing_vector_rank_makes_a_hit_lexical_only(self):
        result = await _search_result((_ranked_row("p.one#s1", lexical_rank=3, vector_rank=None),))

        (hit,) = result.hits
        assert hit.support == "lexical_only"
        assert hit.vector_distance is None
        assert hit.answered_by == "lexical"
        assert result.status == "lexically_supported_candidates"

    async def test_a_missing_lexical_rank_makes_a_hit_semantic_only(self):
        result = await _search_result((_ranked_row("p.one#s1", lexical_rank=None, vector_rank=1),))

        (hit,) = result.hits
        assert hit.support == "semantic_only"
        assert hit.answered_by == "hybrid"
        assert result.status == "semantic_candidates_only"

    async def test_one_lexically_supported_hit_lifts_the_whole_result(self):
        rows = (
            _ranked_row("p.one#s1", lexical_rank=None, vector_rank=1, score=0.9),
            _ranked_row("p.two#s1", lexical_rank=2, vector_rank=None, score=0.4),
        )
        result = await _search_result(rows)

        assert [hit.support for hit in result.hits] == ["semantic_only", "lexical_only"]
        assert result.status == "lexically_supported_candidates"

    async def test_no_rows_is_no_hits_with_a_complete_window(self):
        result = await _search_result(())

        assert result.hits == ()
        assert result.status == "no_hits"
        assert result.window == ResultWindow(returned_items=0, omitted_items=0, truncated=False)

    async def test_a_row_carrying_neither_rank_stays_unassessed(self):
        row = _ranked_row("p.one#s1", lexical_rank=None, vector_rank=None)
        result = await _search_result((row,))

        (hit,) = result.hits
        assert hit.support is None
        assert result.status == "unassessed"

    async def test_the_semantic_only_summary_names_candidates_and_refuses_to_claim_support(self):
        result = await _search_result((_ranked_row("p.one#s1", lexical_rank=None, vector_rank=1),))

        summary = result.next_steps.summary
        assert "nearest semantic candidates" in summary
        assert "evidence unverified" in summary
        assert "supported answer" not in summary
        assert "grounded" not in summary
        joined = " ".join(result.next_steps.options)
        assert f"fetch(unit_id='collection:{_COLLECTION}')" in joined  # browse by structure instead
        assert "do not answer" in joined  # say the knowledge base lacks support


class TestBoundedSnippetsAndResponse:
    """A hit is an excerpt plus an address, and the whole answer fits one response budget."""

    async def test_a_huge_multibyte_section_body_comes_back_as_a_bounded_snippet(self):
        body = "α β γ δ ε ζ η θ — 段落 " * 1000  # a multibyte section body far past any snippet bound
        assert len(body) > 20_000
        rows = tuple(
            _ranked_row(f"p.{index}#s1", lexical_rank=index + 1, vector_rank=index + 1, score=1.0 - index / 100, snippet=body)
            for index in range(12)
        )

        result = await _search_result(rows)

        assert result.hits
        for hit in result.hits:
            assert hit.snippet is not None
            assert len(hit.snippet) <= 480
            assert hit.snippet_truncated is True
            assert hit.snippet.encode("utf-8").decode("utf-8") == hit.snippet  # every cut lands on a code point
        assert len(result.model_dump_json()) <= 12_000
        assert [hit.id for hit in result.hits] == [f"p.{index}#s1" for index in range(len(result.hits))]
        assert result.window.returned_items == len(result.hits)
        assert result.window.truncated is (len(result.hits) < len(rows))
        assert result.next_steps.options[0].startswith(f"fetch(unit_id='section:{_COLLECTION}/p.0#s1')")

    async def test_the_window_counts_exactly_what_the_budget_removed(self):
        body = "z" * 3000
        rows = tuple(
            _ranked_row(f"p.{index}#s1", lexical_rank=index + 1, vector_rank=None, score=1.0 - index / 100, snippet=body)
            for index in range(40)
        )

        result = await _search_result(rows)

        assert result.window.truncated is True
        assert result.window.omitted_items == len(rows) - len(result.hits)
        assert result.window.refused is False
        assert result.window.reason


def _hit(unit_id: str, *, ref: str, title: str | None = "A Page", snippet: str | None = "an excerpt", score: float = 0.5) -> SearchHit:
    """One projected hit, as `bound_search_result` receives it."""
    return SearchHit(
        id=unit_id,
        ref=ref,
        hit_level="section",
        page_id=unit_id.partition("#")[0],
        page_title=title,
        snippet=snippet,
        score=score,
        lexical_rank=1,
        support="lexical_only",
        answered_by="lexical",
    )


_STEPS = NextSteps(summary="2 hit(s), ranked.", options=("fetch(unit_id='section:workdocs/p.one#s1') to read its full text.",))


def _bounded(hits: tuple[SearchHit, ...], *, max_chars: int, next_steps: NextSteps = _STEPS, stamp: KnowledgeBaseStamp | None = None):
    """`bound_search_result` under a stated budget, with the candidate status every fixture here uses."""
    return bound_search_result(hits, status="lexically_supported_candidates", next_steps=next_steps, knowledge_base=stamp, max_chars=max_chars)


class TestTheResponseBudgetBoundary:
    """Identity never shortens, display text does, and a budget nobody can meet refuses cleanly."""

    def test_the_default_budget_is_twelve_thousand_characters(self):
        assert SEARCH_RESULT_CHARS == 12_000

    def test_an_enormous_title_shortens_while_its_ref_stays_byte_identical(self):
        ref = "section:workdocs/p.one#s1"
        hit = _hit("p.one#s1", ref=ref, title="τ" * 50_000)

        result = _bounded((hit,), max_chars=SEARCH_RESULT_CHARS)

        (kept,) = result.hits
        assert kept.ref == ref
        assert kept.ref is not None
        assert kept.ref.encode("utf-8") == ref.encode("utf-8")
        assert kept.id == "p.one#s1"
        assert kept.page_title is not None
        assert len(kept.page_title) < 50_000
        assert kept.page_title.endswith("…")
        assert len(result.model_dump_json()) <= SEARCH_RESULT_CHARS

    def test_an_enormous_ref_refuses_without_echoing_or_cutting_it(self):
        ref = "section:workdocs/" + "q" * 50_000
        hit = _hit("q" * 50_000, ref=ref, title=None, snippet=None)

        result = _bounded((hit,), max_chars=SEARCH_RESULT_CHARS)

        assert result.hits == ()
        assert result.status == "budget_refused"
        assert result.window.refused is True
        assert result.window.returned_items == 0
        assert result.window.omitted_items == 1
        assert result.window.reason
        rendered = result.model_dump_json()
        assert "qqqqqqqqqq" not in rendered  # the oversized identity is neither echoed nor truncated into the answer
        assert len(rendered) <= SEARCH_RESULT_CHARS
        assert result.next_steps.summary
        assert result.next_steps.options

    def test_every_candidate_being_oversized_still_yields_one_bounded_refusal(self):
        hits = tuple(_hit("x" * 20_000 + str(index), ref="section:workdocs/" + "x" * 20_000, title=None, snippet=None) for index in range(4))

        result = _bounded(hits, max_chars=SEARCH_RESULT_CHARS)

        assert result.hits == ()
        assert result.status == "budget_refused"
        assert result.window.omitted_items == 4
        assert len(result.model_dump_json()) <= SEARCH_RESULT_CHARS

    def test_multibyte_title_and_snippet_cuts_stay_valid_unicode(self):
        hit = _hit("p.one#s1", ref="section:workdocs/p.one#s1", title="漢字" * 4000, snippet="🍅🥒" * 4000)

        result = _bounded((hit,), max_chars=2_000)

        (kept,) = result.hits
        for text in (kept.page_title, kept.snippet):
            assert text is not None
            assert text.encode("utf-8").decode("utf-8") == text
            assert "�" not in text
        assert kept.snippet_truncated is True

    def test_the_stamp_window_and_next_steps_are_counted_before_any_hit(self):
        hits = tuple(_hit(f"p.{index}#s1", ref=f"section:workdocs/p.{index}#s1", score=1.0 - index / 100) for index in range(8))
        wordy = NextSteps(summary="A very long summary. " * 40, options=("A very long option. " * 40,))
        stamp = KnowledgeBaseStamp(slug="workdocs", changed_since=0)

        lean = _bounded(hits, max_chars=2_400)
        heavy = _bounded(hits, max_chars=2_400, next_steps=wordy, stamp=stamp)

        assert len(heavy.hits) < len(lean.hits)  # the envelope's own bytes came out of the hit budget
        assert len(heavy.model_dump_json()) <= 2_400

    def test_a_model_at_the_cap_is_kept_and_the_candidate_that_crosses_it_is_omitted(self):
        hits = tuple(_hit(f"p.{index}#s1", ref=f"section:workdocs/p.{index}#s1", title=None, snippet=None, score=1.0 - index / 100) for index in range(3))
        whole = _bounded(hits, max_chars=100_000)
        exact = len(whole.model_dump_json())

        at_the_cap = _bounded(hits, max_chars=exact)
        one_over = _bounded(hits, max_chars=exact - 1)

        assert at_the_cap == whole
        assert len(at_the_cap.hits) == 3
        assert at_the_cap.window == ResultWindow(returned_items=3, omitted_items=0, truncated=False)
        assert len(one_over.hits) == 2
        assert one_over.window.truncated is True
        assert one_over.window.omitted_items == 1
        assert len(one_over.model_dump_json()) <= exact - 1
        assert [hit.id for hit in one_over.hits] == ["p.0#s1", "p.1#s1"]  # rank order, never a reshuffle


class TestARunawayQueryNeverBreaksTheBudget:
    """The query text is the caller's own and rides into the summary, so it is bounded like any display text."""

    async def test_a_huge_query_is_echoed_bounded_and_the_answer_still_fits(self):
        result = await _search_result((_ranked_row("p.one#s1", lexical_rank=1, vector_rank=None),), query="ω" * 20_000)

        assert len(result.model_dump_json()) <= SEARCH_RESULT_CHARS
        assert "…" in result.next_steps.summary
        assert result.hits  # the oversized query cost the answer nothing

    async def test_a_huge_query_with_no_hits_also_fits(self):
        result = await _search_result((), query="ω" * 20_000)

        assert result.status == "no_hits"
        assert len(result.model_dump_json()) <= SEARCH_RESULT_CHARS


class TestAnOversizedKnowledgeBaseNameNeverBreaksTheBudget:
    """The `collection` argument is the agent's own text and rides into a no-hits answer's every line."""

    async def test_a_huge_collection_name_still_leaves_a_bounded_answer(self):
        oversized = "v" * 50_000

        result = await _search_result((), collection=oversized)

        assert result.status == "no_hits"
        rendered = result.model_dump_json()
        assert len(rendered) <= SEARCH_RESULT_CHARS
        assert "vvvvvvvvvv" not in rendered  # neither echoed nor clipped into a ref that would not read
        assert result.next_steps.summary
        assert result.next_steps.options

    async def test_a_workable_collection_name_still_gets_its_map_option(self):
        result = await _search_result((), collection="workdocs")

        assert "fetch(unit_id='collection:workdocs')" in " ".join(result.next_steps.options)


class TestNextStepsNameOnlyTheProfilesOwnTools:
    """A next move is a call the caller can make: the registered profile decides which names appear.

    The handlers here are the real graph ones over a fake gateway, so the next-move block under test
    is the one an eval run and the code-mode sandbox actually receive — the surface where a strategy
    is scored.
    """

    def _handlers(self) -> RetrievalHandlers:
        gateway = FakeSqlGateway()
        return RetrievalHandlers(
            search=FakeSearchHandler(),
            fetch=FakeFetchHandler(),
            graph_expand=GraphExpandHandler(gateway, _COLLECTION),
            find_paths=GraphPathsHandler(gateway, _COLLECTION),
        )

    async def test_a_graph_only_expansion_never_offers_a_search(self):
        toolset = build_retrieval_toolset(self._handlers(), only=resolve_strategy("graph-only").tools, collection=_COLLECTION)

        result = await _call(toolset, "graph_expand", {"seed_ids": ["ent.missing"]})

        assert isinstance(result, GraphResult)
        assert result.next_steps is not None
        assert result.next_steps.options
        assert all("search(" not in option for option in result.next_steps.options)

    async def test_the_full_profile_still_offers_the_search_that_grounds_a_seed(self):
        toolset = build_retrieval_toolset(self._handlers(), collection=_COLLECTION)

        result = await _call(toolset, "graph_expand", {"seed_ids": ["ent.missing"]})

        assert isinstance(result, GraphResult)
        assert result.next_steps is not None
        assert any("search(" in option for option in result.next_steps.options)

    async def test_a_graph_only_route_search_never_offers_a_search(self):
        toolset = build_retrieval_toolset(self._handlers(), only=resolve_strategy("graph-only").tools, collection=_COLLECTION)

        result = await _call(toolset, "find_paths", {"from_id": "ent.a", "to_id": "ent.b"})

        assert isinstance(result, GraphPathsResult)
        assert result.next_steps.options
        assert all("search(" not in option for option in result.next_steps.options)
