"""The ports of retrieval: the read-only SQL gateway, the feedback sink, the stamp source and the four tool handlers.

A host implements each port. A concrete handler satisfies its port structurally through its `__call__`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .domain import (
    FeedbackCategory,
    Fetch,
    GraphExpand,
    GraphPaths,
    GraphPathsResult,
    GraphResult,
    KnowledgeBaseStamp,
    SchemaDescription,
    Search,
    SqlResult,
)
from .names import TOOL_NAMES


class FeedbackSink(Protocol):
    """Records one piece of agent feedback. A consumer wires an append-only sink."""

    async def __call__(self, *, category: FeedbackCategory, message: str, tool: str | None) -> None:
        """Record one piece of agent feedback.

        Args:
            category: The kind of feedback.
            message: The free-text feedback.
            tool: The tool the feedback is about, or `None`.
        """
        ...


type StampSource = Callable[[], Awaitable[KnowledgeBaseStamp]]
"""Returns the stamp for an answer. It is called fresh on every answer, so the count is never stale."""


@runtime_checkable
class SqlGateway(Protocol):
    """Read-only access to a knowledge base database.

    A read-only adapter enforces read-only access and a server-side statement timeout. The gateway
    applies the row `cap`. An admin-connection adapter also satisfies this port, for the one read that
    needs tables an ordinary reader role cannot see.
    """

    async def rows(self, sql: str, params: Mapping[str, object] | None = ..., *, cap: int = 50) -> SqlResult:
        """Run `sql` with bound `params` and return at most `cap` rows.

        Args:
            sql: The read-only SQL to run.
            params: The bound parameters, or `None` for none.
            cap: The most rows to return.

        Returns:
            The rows, with a truncation note when more rows matched than `cap`.
        """
        ...

    async def api_objects(self) -> SchemaDescription:
        """Describe every object of the retrieval surface from the database catalog.

        Returns:
            The description of every retrieval object.
        """
        ...


# -- one single-method slice per handler -------------------------------------


class SupportsSearch[S: Search](Protocol):
    """The handler slice the search tool drives, generic over the search-message type."""

    async def __call__(self, query: S) -> SqlResult:
        """Run the search for `query` and return its row-capped result.

        Args:
            query: The search to run.

        Returns:
            The row-capped result.
        """
        ...


class SupportsFetch(Protocol):
    """The handler slice the fetch tool drives."""

    async def __call__(self, query: Fetch) -> SqlResult:
        """Fetch one unit by id and return its row-capped result.

        Args:
            query: The unit to fetch.

        Returns:
            The row-capped result.
        """
        ...


class SupportsGraphExpand(Protocol):
    """The handler slice the expand tool drives."""

    async def __call__(self, query: GraphExpand, *, tools: frozenset[str] = TOOL_NAMES) -> GraphResult:
        """Expand the graph from the seed set and return the reached subgraph.

        The answer's next move names only tools in `tools`, so a caller holding a subset is never sent to a tool it does not hold.

        Args:
            query: The seed set and how far to expand.
            tools: The tools the calling surface registered.

        Returns:
            The reached subgraph.
        """
        ...


class SupportsFindPaths(Protocol):
    """The handler slice the paths tool drives."""

    async def __call__(self, query: GraphPaths, *, tools: frozenset[str] = TOOL_NAMES) -> GraphPathsResult:
        """Find the ranked typed paths between two nodes, bounded and windowed.

        The answer's next move names only tools in `tools`, as `SupportsGraphExpand` does.

        Args:
            query: The two nodes and the path bounds.
            tools: The tools the calling surface registered.

        Returns:
            The ranked paths.
        """
        ...


@dataclass(frozen=True)
class RetrievalHandlers[S: Search]:
    """The four retrieval handlers the tools wrap, one single-method port each.

    A concrete handler satisfies each port structurally through its `__call__`. The caller builds it
    once and both registration paths use it. It is generic over the search-message type `S`, so a
    caller that extends `Search` with its own facets keeps a precise type.
    """

    search: SupportsSearch[S]
    fetch: SupportsFetch
    graph_expand: SupportsGraphExpand
    find_paths: SupportsFindPaths
