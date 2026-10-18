"""The retrieval toolset builder: four tools defined once, registered on a pydantic-ai toolset or a FastMCP server.

`search` finds candidate units, `fetch` reads one unit's full text, and `graph_expand` and `find_paths` walk the
knowledge graph. `build_retrieval_toolset` registers them on a `FunctionToolset` an agent binds, and `register_mcp_tools`
registers them on a FastMCP server. Both surfaces call the handlers the caller passes and return the shared result models
(`list[SearchHit]`, `FetchResult`, `GraphResult`, `GraphPathsResult`) as structured values, never as a JSON string.
A caller passes its own `Search` subclass as `search_type` to add facets, and passes every tool description, keyed by
tool name. This module does no database or file I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import TypeAdapter
from pydantic_ai import FunctionToolset, ModelRetry

from .domain import Fetch, FetchResult, GraphExpand, GraphPaths, GraphPathsResult, GraphResult, Search, SearchHit
from .names import FETCH, FIND_PATHS, GRAPH_EXPAND, SEARCH, TOOL_MAX_RETRIES, TOOL_NAMES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mcp.server.fastmcp import FastMCP

    from .internal_contracts import RetrievalHandlers, StampSource, SupportsFetch

# The tools take no deps because the handlers are closed over at build time. `AgentDepsT` is
# contravariant, so `object` lets an agent with any deps type take this toolset.
type RetrievalToolset = FunctionToolset[object]

# One validator shared by every MCP `search` call.
_HITS_ADAPTER = TypeAdapter(list[SearchHit])


# -- core helpers: the single source of truth per tool -----------------------


async def _search[S: Search](
    handlers: RetrievalHandlers[S], query: str, k: int = 12, *, bm25_only: bool = False, search_type: type[S]
) -> list[SearchHit]:
    """Run a search and return the handler's rows as ranked hits."""
    result = await handlers.search(search_type(query=query, k=k, hybrid=not bm25_only))
    return [SearchHit.model_validate(dict(row)) for row in result.rows]


async def fetch_one(handler: SupportsFetch, unit_id: str) -> FetchResult | None:
    """Fetch one unit's full text through `handler` alone, `None` when no unit matched.

    It needs only the fetch handler, so a surface that serves `fetch` without the other tools can call it.

    Args:
        handler: The fetch handler slice to drive.
        unit_id: The unit id to fetch.

    Returns:
        The fetched unit as a `FetchResult`, or `None` when the id matched no row.
    """
    result = await handler(Fetch(id=unit_id))
    if not result.rows:
        return None
    return FetchResult.model_validate(dict(result.rows[0]))


async def fetch_unit[S: Search](handlers: RetrievalHandlers[S], unit_id: str) -> FetchResult | None:
    """Fetch one unit's full text by id, `None` when no unit matched (an unknown or typo id).

    Args:
        handlers: The retrieval handlers whose `fetch` is called.
        unit_id: The unit id to fetch (an id returned by `search`).

    Returns:
        The fetched unit as a `FetchResult`, or `None` when the id matched no row.
    """
    return await fetch_one(handlers.fetch, unit_id)


async def _graph_expand[S: Search](
    handlers: RetrievalHandlers[S], seed_ids: list[str], hops: int = 2, *, tools: frozenset[str] = TOOL_NAMES
) -> GraphResult:
    """Expand the graph from `seed_ids` out to `hops` and return the reached subgraph."""
    return await handlers.graph_expand(GraphExpand(seed_ids=tuple(seed_ids), hops=hops), tools=tools)


async def _find_paths[S: Search](
    handlers: RetrievalHandlers[S], from_id: str, to_id: str, max_hops: int, *, tools: frozenset[str] = TOOL_NAMES
) -> GraphPathsResult:
    """Find the ranked typed paths from `from_id` to `to_id` within `max_hops` and return the handler's bounded result."""
    return await handlers.find_paths(GraphPaths(from_id=from_id, to_id=to_id, max_hops=max_hops), tools=tools)


# -- adapter 1: the pydantic-ai FunctionToolset ------------------------------


def build_retrieval_toolset[S: Search](
    handlers: RetrievalHandlers[S],
    *,
    available: frozenset[str] | None = None,
    descriptions: Mapping[str, str],
    only: frozenset[str] | None = None,
    search_hybrid: bool | None = None,
    search_type: type[S] = Search,
    stamp: StampSource | None = None,
) -> RetrievalToolset:
    """Build the deps-agnostic `FunctionToolset` of the retrieval tools (optionally a subset).

    The tools close over `handlers`, so nothing rides the agent's run `deps`, and return the typed result models.
    The `fetch` tool raises `ModelRetry` on an unknown id, so the model finds the id again with `search`.

    Args:
        handlers: The retrieval handlers the tools call.
        available: The tool names the finished surface serves, when the caller registers some of them
            itself rather than through this builder. The graph answers' next moves name these and no
            others. `None` (the default) takes the surface to be exactly what this call registers.
        descriptions: The final per-tool description strings keyed by canonical tool name (`SEARCH` /
            `FETCH` / `GRAPH_EXPAND` / `FIND_PATHS`). Every registered tool reads its string here, so
            the map must carry a key for each tool the selection registers. The caller resolves
            defaults, overrides and the search-mode text first.
        only: When given, register only the named tools (a subset of `TOOL_NAMES`). `None` (the
            default) registers all four.
        search_hybrid: Forces the `search` tool's leg as a fixed variable, dropping the agent-chosen
            `bm25_only` arg. `True` runs vector plus keyword, `False` runs keyword-only. `None` (the
            default) keeps the agent-controlled search with its `bm25_only` flag.
        search_type: The concrete search-message type the `search` tool constructs (a caller's
            `Search` subclass). Defaults to the generic `Search`.
        stamp: The host's way of naming the knowledge base that answered and how fresh its indexed copy
            is. When given, its result rides every `fetch` answer as `knowledge_base`. `None` (the default)
            leaves `knowledge_base` unset.

    Returns:
        A `FunctionToolset` exposing the selected subset of `search` / `fetch` / `graph_expand` /
        `find_paths`.
    """
    toolset: RetrievalToolset = FunctionToolset()
    selected = TOOL_NAMES if only is None else only
    served = selected if available is None else available

    async def fetch(unit_id: str) -> FetchResult:
        unit = await fetch_unit(handlers, unit_id)
        if unit is None:
            msg = f"No unit found with id {unit_id!r}. Re-ground the id with `search` and try again."
            raise ModelRetry(msg)
        return unit if stamp is None else unit.model_copy(update={"knowledge_base": await stamp()})

    async def graph_expand(seed_ids: list[str], hops: int = 2) -> GraphResult:
        return await _graph_expand(handlers, seed_ids, hops, tools=served)

    async def find_paths(from_id: str, to_id: str, max_hops: int = 4) -> GraphPathsResult:
        return await _find_paths(handlers, from_id, to_id, max_hops, tools=served)

    # `search` goes through a helper because its signature depends on the forced mode. One
    # `add_function` call per tool keeps each tool's exact parameter signature.
    if SEARCH in selected:
        _register_search_tool(toolset, handlers, search_hybrid=search_hybrid, descriptions=descriptions, search_type=search_type)
    if FETCH in selected:
        toolset.add_function(fetch, takes_ctx=False, name=FETCH, description=descriptions[FETCH], retries=TOOL_MAX_RETRIES)
    if GRAPH_EXPAND in selected:
        toolset.add_function(graph_expand, takes_ctx=False, name=GRAPH_EXPAND, description=descriptions[GRAPH_EXPAND], retries=TOOL_MAX_RETRIES)
    if FIND_PATHS in selected:
        toolset.add_function(find_paths, takes_ctx=False, name=FIND_PATHS, description=descriptions[FIND_PATHS], retries=TOOL_MAX_RETRIES)
    return toolset


def _register_search_tool[S: Search](
    toolset: RetrievalToolset, handlers: RetrievalHandlers[S], *, search_hybrid: bool | None, descriptions: Mapping[str, str], search_type: type[S]
) -> None:
    """Add the `search` tool to `toolset`, agent-controlled when `search_hybrid` is None and with a fixed leg otherwise."""
    if search_hybrid is None:

        async def search_agent_choice(query: str, k: int = 12, bm25_only: bool = False) -> list[SearchHit]:
            return await _search(handlers, query, k, bm25_only=bm25_only, search_type=search_type)

        toolset.add_function(search_agent_choice, takes_ctx=False, name=SEARCH, description=descriptions[SEARCH], retries=TOOL_MAX_RETRIES)
        return

    async def search_forced(query: str, k: int = 12) -> list[SearchHit]:
        return await _search(handlers, query, k, bm25_only=search_hybrid is False, search_type=search_type)

    toolset.add_function(search_forced, takes_ctx=False, name=SEARCH, description=descriptions[SEARCH], retries=TOOL_MAX_RETRIES)


# -- adapter 2: the FastMCP server -------------------------------------------


def register_mcp_tools[S: Search](
    server: FastMCP,
    handlers: RetrievalHandlers[S],
    *,
    available: frozenset[str] | None = None,
    descriptions: Mapping[str, str],
    only: frozenset[str] | None = None,
    search_hybrid: bool | None = None,
    search_type: type[S] = Search,
    stamp: StampSource | None = None,
) -> None:
    """Register the same four retrieval tools on a FastMCP `server`.

    The tools return the same structured values the `FunctionToolset` returns, and FastMCP delivers each
    as `structuredContent` under its `outputSchema`. `search` wraps its hits in an object under a `"hits"`
    key. A `fetch` of an unknown id raises `ToolError`. These tools carry no retry cap, so the MCP client
    decides whether to retry, and `TOOL_MAX_RETRIES` applies only to the pydantic-ai toolset.

    Args:
        server: The `FastMCP` instance to register the tools on.
        handlers: The retrieval handlers the tools call.
        available: The tool names the finished server serves, when the caller registers some of them
            itself rather than through this function. The graph answers' next moves name these and no
            others. `None` (the default) takes the server to be exactly what this call registers.
        descriptions: The final per-tool description strings keyed by canonical tool name (see
            `build_retrieval_toolset`). The map must carry a key for each registered tool.
        only: When given, register only the named tools (a subset of `TOOL_NAMES`). `None` (the
            default) registers all four.
        search_hybrid: When `"search"` is registered, forces the search leg as a fixed variable:
            `True` runs vector plus keyword and `False` runs keyword-only (both drop the `bm25_only`
            arg), `None` (the default) keeps the agent-controlled search with its `bm25_only` arg.
        search_type: The concrete search-message type the `search` tool constructs. Defaults to the
            generic `Search`.
        stamp: The host's way of naming the knowledge base that answered and how fresh its indexed copy
            is. When given, its result rides every `fetch` answer as `knowledge_base`. `None` (the default)
            leaves `knowledge_base` unset.
    """
    served = (TOOL_NAMES if only is None else only) if available is None else available
    if only is None or SEARCH in only:
        _add_search_mcp_tool(server, handlers, search_hybrid=search_hybrid, descriptions=descriptions, search_type=search_type)

    async def fetch(unit_id: str) -> FetchResult:
        unit = await fetch_unit(handlers, unit_id)
        if unit is None:
            msg = f"No unit found with id {unit_id!r}. Re-ground the id with search and try again."
            raise ToolError(msg)
        return unit if stamp is None else unit.model_copy(update={"knowledge_base": await stamp()})

    async def graph_expand(seed_ids: list[str], hops: int = 2) -> GraphResult:
        return await _graph_expand(handlers, seed_ids, hops, tools=served)

    async def find_paths(from_id: str, to_id: str, max_hops: int = 4) -> GraphPathsResult:
        return await _find_paths(handlers, from_id, to_id, max_hops, tools=served)

    candidates = ((fetch, FETCH), (graph_expand, GRAPH_EXPAND), (find_paths, FIND_PATHS))
    for tool, name in candidates:
        if only is None or name in only:
            server.add_tool(tool, name=name, description=descriptions[name])


def _add_search_mcp_tool[S: Search](
    server: FastMCP, handlers: RetrievalHandlers[S], *, search_hybrid: bool | None, descriptions: Mapping[str, str], search_type: type[S]
) -> None:
    """Add the `search` tool to `server`, agent-controlled when `search_hybrid` is None and with a fixed leg otherwise."""
    if search_hybrid is None:

        async def search_agent(query: str, k: int = 12, bm25_only: bool = False) -> dict[str, Any]:
            hits = await _search(handlers, query, k, bm25_only=bm25_only, search_type=search_type)
            return {"hits": _HITS_ADAPTER.dump_python(hits, mode="json")}

        server.add_tool(search_agent, name=SEARCH, description=descriptions[SEARCH])
        return

    forced_hybrid = bool(search_hybrid)

    async def search_forced(query: str, k: int = 12) -> dict[str, Any]:
        hits = await _search(handlers, query, k, bm25_only=not forced_hybrid, search_type=search_type)
        return {"hits": _HITS_ADAPTER.dump_python(hits, mode="json")}

    server.add_tool(search_forced, name=SEARCH, description=descriptions[SEARCH])
