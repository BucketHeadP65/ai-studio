"""The knowledge base as pydantic-ai capabilities: the read tools and the SQL tools, with their instructions.

`SqlKnowledgeBase` serves the same two SQL tools as the MCP server, with the same row cap and refusal
text, to a host that imports engram in-process. `RetrievalKnowledgeBase` serves the read tools over one
or several knowledge bases.

Each capability's `get_instructions` travels with its toolset. `SqlKnowledgeBase` composes, in order,
the caller's `domain` sentence, the `guidance()` text read on every request, and the tooling text
rendered under the names its tools are registered with.

With several knowledge bases, a `search` hit carries the collection-qualified `ref` of the base that
ranked it, and that ref reads back from that same base, even where another base holds the same bare id.

Both classes also offer `list_tools`, `filtered` and `direct_call_tool`, the surface a workflow host
reads a step's grantable tools from.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar, Protocol

from core.ai.disclosure.contracts import ChildSummary, DisclosureEnvelope, DisclosureMode
from core.ai.retrieval.contracts import FETCH, FIND_PATHS, GRAPH_EXPAND, SEARCH, TOOL_MAX_RETRIES, fetch_unit
from mcp import types as mcp_types
from pydantic_ai import FunctionToolset
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT

from engram.skills.contracts import SKILLS

from .dials import DEFAULT_DIALS, ReadDials
from .domain import (
    FetchResult,
    GraphExpand,
    GraphPaths,
    GraphPathsResult,
    GraphResult,
    Period,
    Search,
    SearchHit,
    SearchResult,
    period_end,
    period_start,
)
from .envelope import (
    BadRefError,
    WrongCollectionError,
    fetch_description,
    flat_fetch_description,
    library_fetch_description,
    parse_ref,
    qualify_ref,
)
from .infrastructure.gateway import read_snapshot
from .queries import expansion_hops, graph_next_steps, paths_next_steps, route_hops, routed_graph_paths, routed_graph_result
from .sql_tools import DESCRIBE_SCHEMA_DESCRIPTION, RUN_SQL_DESCRIPTION, make_describe_schema, make_run_sql, tooling_instructions
from .tools import (
    DATE_RANGE_CLAUSE,
    FIND_PATHS_DESCRIPTION,
    GRAPH_EXPAND_DESCRIPTION,
    RefRefusal,
    bound_search_result,
    declared_facets_clause,
    elide_k,
    leveled_fetch,
    no_map_refusal,
    no_unit_refusal,
    refuse_duplicate_collections,
    refuse_undeclared_facets,
    search_description,
    search_hits,
    search_next_steps,
    search_result_status,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from datetime import date
    from pathlib import Path

    from core.ai.embedding import QueryEmbedder
    from core.ai.retrieval.contracts import RetrievalHandlers, StampSource
    from pydantic_ai import RunContext
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.toolsets import AbstractToolset

    from engram.authoring.contracts import AuthoringTools
    from engram.indexing.contracts import RetrievalSwitches

    from .dials import DialsSource
    from .domain import SqlGateway
    from .queries import DescribeSchemaHandler
    from .tools import FetchEnvelope, FetchUnit

    NativeTool = Callable[..., Awaitable[object]]
    """One built tool callable, as this class dispatches to it: keyword args in, its payload out."""

    LeveledRead = Callable[[str], Awaitable["FetchEnvelope | FetchUnit"]]
    """One knowledge base's leveled read: a ref in, its envelope (or flat unit) plus stamp out."""

    _Facets = dict[str, list[str]] | None
    """A `search` call's facet filter: a facet key mapped to its values, `None` when the call passed none."""

    class _RunOne(Protocol):
        """The sole-surface search body the published `search` signatures close over."""

        async def __call__(self, query: str, k: int, *, since: str | None, until: str | None, facets: _Facets) -> SearchResult:
            """Search the one surface for `query`, capped at `k` hits."""
            ...

    class _RunMany(Protocol):
        """The multi-surface search body, given whichever `knowledge_base` the agent named."""

        async def __call__(
            self, query: str, k: int, knowledge_base: str | None, *, since: str | None, until: str | None, facets: _Facets
        ) -> SearchResult:
            """Search the surfaces `knowledge_base` reaches for `query`, capped at `k` hits."""
            ...

DEFAULT_TOOL_NAMES = ("run_sql", "describe_schema")
"""The names the MCP server registers, so a host that renames nothing matches it exactly."""

_GRAPH_TOOL_NAMES = frozenset({GRAPH_EXPAND, FIND_PATHS})
"""The two graph tools `RetrievalKnowledgeBase` exposes only over a surface that has earned them."""

_READ_TOOL_NAMES = frozenset({SEARCH, FETCH, GRAPH_EXPAND, FIND_PATHS})
"""The tools a graph answer may name in its next move."""

_UNNAMED = "\x00"
"""A collection name no knowledge base can declare, so `parse_ref` answering it means the ref named none."""

_NAME_TO_SEARCH = "Name the knowledge base to search: pass `knowledge_base`."
"""What a `search` naming no knowledge base is told to do, where naming one is required."""

_NAME_TO_READ = (
    "Name the knowledge base to read: pass `knowledge_base`, or pass back a `ref` from a `search` hit or a listing, "
    "which names its own."
)
"""What a `fetch` naming no knowledge base is told to do."""


def skills_directory() -> Path:
    """The knowledge base skills engram ships, as a path a host mounts on its own skills capability.

    Engram does not mount a skills capability itself. A host that offers the read tools mounts this directory.

    Returns:
        The directory holding the shipped skills.
    """
    return SKILLS


class ToolsetCapability(AbstractCapability[AgentDepsT]):
    """A capability that serves a prebuilt toolset and runs its tools by name outside any agent."""

    _toolset: FunctionToolset[AgentDepsT]
    _calls: dict[str, NativeTool]
    _served_by: ClassVar[str]
    """How a refusal names what serves the tools."""

    def filtered(
        self, filter_func: Callable[[RunContext[AgentDepsT], ToolDefinition], bool | Awaitable[bool]]
    ) -> AbstractToolset[AgentDepsT]:
        """A view admitting only the tools `filter_func` accepts.

        Args:
            filter_func: Answers whether a tool is admitted for a run.

        Returns:
            The filtered toolset.
        """
        return self._toolset.filtered(filter_func)

    async def direct_call_tool(
        self, name: str, args: dict[str, object], *, metadata: dict[str, object] | None = None, use_task: bool = False
    ) -> object:
        """Run one tool by name, outside any agent.

        Arguments are validated against the tool's own schema before the call. `metadata` and
        `use_task` are accepted and ignored.

        Args:
            name: The tool to run, under this instance's names.
            args: Its keyword arguments, as the caller built them.
            metadata: Accepted for signature compatibility, unused.
            use_task: Accepted for signature compatibility, unused.

        Returns:
            Whatever the tool answered, such as the rows envelope, the M-Schema text, the ranked hits or a unit.

        Raises:
            KeyError: When `name` is not one of the tools this capability serves.
        """
        del metadata, use_task
        tool = self._toolset.tools.get(name)
        if tool is None:
            msg = f"unknown tool {name!r}: {self._served_by} serves {', '.join(sorted(self._toolset.tools))}"
            raise KeyError(msg)
        validated: dict[str, object] = tool.function_schema.validator.validate_python(dict(args))
        return await self._calls[name](**validated)


@dataclass
class SqlKnowledgeBase(ToolsetCapability[AgentDepsT]):
    """Read-only SQL over one knowledge base, with the doctrine for using it attached."""

    gateway: SqlGateway
    """The read-only gateway every statement goes through. It enforces the row cap and the role."""
    describe_handler: DescribeSchemaHandler
    """Renders the `api` surface's schema: a compact catalog with no `objects` named, full column
    detail for named ones. The agent's one route to a column name."""
    domain: str
    """What knowledge this knowledge base holds, in the caller's own words. It is required."""
    embed_query: QueryEmbedder | None = None
    """Embeds a `vector_query => '<text>'` for the hybrid leg. `None` degrades it to BM25 with a note."""
    guidance: Callable[[], str | None] | None = None
    """How to retrieve well from this knowledge base, read on every request. `None` when there is none."""
    tool_names: tuple[str, str] = DEFAULT_TOOL_NAMES
    """The names the `run_sql` and `describe_schema` tools are registered under, in that order."""
    retry_refusals: bool = False
    """Whether a refused statement or schema request goes back to the model as a retry, up to `TOOL_MAX_RETRIES` in a row.
    `False` fails the call."""

    _served_by: ClassVar[str] = "this knowledge base"
    _toolset: FunctionToolset[AgentDepsT] = field(init=False, repr=False)
    _calls: dict[str, NativeTool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the two tools once, so every caller dispatches the same callables."""
        run_sql_name, describe_schema_name = self.tool_names
        run_sql = make_run_sql(self.gateway, self.embed_query, retry_refusals=self.retry_refusals)
        describe_schema = make_describe_schema(self.describe_handler, retry_refusals=self.retry_refusals)
        retries = TOOL_MAX_RETRIES if self.retry_refusals else None
        toolset = FunctionToolset[AgentDepsT]()
        toolset.add_function(run_sql, takes_ctx=False, name=run_sql_name, description=RUN_SQL_DESCRIPTION, retries=retries)
        toolset.add_function(
            describe_schema, takes_ctx=False, name=describe_schema_name, description=DESCRIBE_SCHEMA_DESCRIPTION, retries=retries
        )
        self._toolset = toolset
        self._calls = {run_sql_name: run_sql, describe_schema_name: describe_schema}

    def get_toolset(self) -> FunctionToolset[AgentDepsT]:
        """The two SQL tools this capability contributes.

        Returns:
            The toolset holding `run_sql` and `describe_schema` under this instance's names.
        """
        return self._toolset

    def get_instructions(self) -> Callable[[RunContext[AgentDepsT]], str]:
        """The instructions: what the knowledge base holds, how to retrieve from it well, and how the tools work.

        The guidance is read when each request is built.

        Returns:
            A callable composing the domain sentence, the guidance and the tooling text.
        """

        def compose(_ctx: RunContext[AgentDepsT]) -> str:
            voices = (self.domain, self._guidance(), tooling_instructions(*self.tool_names))
            return "\n\n".join(voice for voice in voices if voice)

        return compose

    def _guidance(self) -> str | None:
        """This knowledge base's retrieval guidance for the request being built, or `None` if it has none."""
        return self.guidance() if self.guidance is not None else None

    async def list_tools(self) -> list[mcp_types.Tool]:
        """The tools this knowledge base offers, in MCP's `Tool` shape.

        Returns:
            One MCP `Tool` per registered tool, with its name, description and input schema.
        """
        return [
            mcp_types.Tool(name=tool.name, description=tool.description, inputSchema=dict(tool.tool_def.parameters_json_schema))
            for tool in self._toolset.tools.values()
        ]


class EnvelopeQueries(Protocol):
    """The slice of `EnvelopeHandler` the read tools drive: leveled reads under a declared mode."""

    @property
    def mode(self) -> DisclosureMode:
        """The knowledge base's declared disclosure mode."""
        ...

    async def __call__(self, ref: str) -> DisclosureEnvelope:
        """Render `ref`'s envelope, raising `BadRefError` on any ref outside the leveled surface.

        Args:
            ref: The ref of the unit or listing to render.

        Returns:
            The envelope.

        Raises:
            BadRefError: When `ref` is outside the leveled surface.
        """
        ...


@dataclass(frozen=True)
class ReadSurface:
    """One knowledge base behind the shared read tools: its handlers, its envelope, its voice."""

    name: str
    """What `search`'s `knowledge_base` argument and a person both call this knowledge base (the spec's slug)."""
    collection: str
    """The knowledge base partition its rows live in."""
    handlers: RetrievalHandlers[Search]
    """The search / fetch / graph handlers over this knowledge base's gateway."""
    envelope: EnvelopeQueries
    """The leveled reads, under this knowledge base's own disclosure declarations."""
    domain: str
    """What knowledge this knowledge base holds, in its owner's words."""
    stamp: StampSource | None = None
    """This knowledge base's own name and freshness, read on every answer it gives.

    Every `search` and `fetch` this base answers carries it. `None` leaves this base's answers unstamped."""
    guidance: Callable[[], str | None] | None = None
    """How to retrieve well from this knowledge base, read per request. `None` when the deployment has none."""
    hybrid: bool = True
    """Whether this surface's search runs the vector leg. The agent cannot change it."""
    switch_source: Callable[[], Awaitable[RetrievalSwitches]] | None = None
    """The knowledge base's stored retrieval switches, read on every call. Set only for an auto-mode knowledge base.

    `.embeddings` gates the vector leg and `.graph` gates the graph tools. `None` consults no switch: the
    vector leg runs whenever `hybrid` and the dial allow it, and the graph tools never appear."""
    facet_keys: tuple[str, ...] = ()
    """The facet keys this knowledge base's schema declares, the only keys `search` filters it by in `facets`.

    A call that searches this base refuses any other key. Empty (the default) refuses every key."""


@dataclass
class RetrievalKnowledgeBase(ToolsetCapability[AgentDepsT]):
    """Knowledge bases as read tools: `search`, a leveled `fetch`, and earned `graph_expand` and `find_paths`.

    However many knowledge bases it holds, the agent sees the same `search` and `fetch`. With several,
    `search` takes a `knowledge_base` argument. `fetch` takes one too while `cross_search` is off, and
    with it on a no-argument `fetch` lists the knowledge bases. `search` takes `facets` over the keys
    every knowledge base it searches declares (`ReadSurface.facet_keys`).
    `graph_expand` and `find_paths` appear only while some auto-mode surface has its stored graph switch
    on, decided on every request. A ref that names its own collection is answered by that base alone.
    A bare id falls back to the first surface that recognizes it.
    """

    surfaces: tuple[ReadSurface, ...]
    """The knowledge bases, in the order they are listed and searched."""
    k_source: Callable[[], int] | None = None
    """The row cap when the host owns it, read on every `search` call.

    Set, `search` takes no `k` and its description never mentions one. `None` keeps `k` an argument
    the agent passes, with its own default."""

    cross_search: bool = False
    """Whether one `search` or `fetch` call may read more than one knowledge base.

    Off (the default), a capability over several knowledge bases makes `knowledge_base` a required argument on
    both tools, and `knowledge_base="all"` or an unknown name is refused. On, `knowledge_base` is optional.
    An omitted one reads every base, or the host's default when `default_knowledge_base_source` names one,
    and `knowledge_base="all"` is accepted. A capability over a single knowledge base has no `knowledge_base`
    argument either way."""

    default_knowledge_base_source: Callable[[], str | None] | None = None
    """The surface an omitted `knowledge_base` searches when the host owns one, read on every call.

    Consulted only while `cross_search` is on. A returned name that matches a surface scopes the call
    to it. `None`, or a name that is not a surface, reads every surface."""

    dials_source: DialsSource | None = None
    """The live read modes when the host owns them, read on every call.

    A dial turned under a running host changes the next tool call and its tool descriptions. `None`
    serves the defaults: hybrid where a surface has an embedder, leveled reads."""

    authoring: AuthoringTools | None = None
    """The authoring tools this capability also serves. `None` (the default) keeps it read-only."""

    _served_by: ClassVar[str] = "this reader"
    _toolset: FunctionToolset[AgentDepsT] = field(init=False, repr=False)
    _calls: dict[str, NativeTool] = field(init=False, repr=False)
    _reads: dict[str, LeveledRead] = field(init=False, repr=False)
    _graph_capable: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the read tools and any authoring tools once, so every caller dispatches the same callables.

        `graph_expand` and `find_paths` are registered only when some surface has a `switch_source`.

        Raises:
            ValueError: With no surface at all, or with two surfaces claiming one collection.
        """
        if not self.surfaces:
            msg = "RetrievalKnowledgeBase needs at least one surface: a reader with no knowledge base answers nothing."
            raise ValueError(msg)
        refuse_duplicate_collections(self.surfaces)
        self._reads = {
            surface.collection: leveled_fetch(
                surface.handlers, surface.envelope, surface.collection, stamp=surface.stamp, knowledge_base=surface.name
            )
            for surface in self.surfaces
        }
        search = self._search_tool()
        fetch = self._fetch_tool()
        toolset = FunctionToolset[AgentDepsT]()
        toolset.add_function(search, takes_ctx=False, name=SEARCH, description=self._search_description(), retries=TOOL_MAX_RETRIES)
        toolset.add_function(fetch, takes_ctx=False, name=FETCH, description=self._fetch_description(), retries=TOOL_MAX_RETRIES)
        calls: dict[str, NativeTool] = {SEARCH: search, FETCH: fetch}
        if self.authoring is not None:
            for fn, name, description in self.authoring.tools():
                toolset.add_function(fn, takes_ctx=False, name=name, description=description, retries=TOOL_MAX_RETRIES)
                calls[name] = fn
        self._graph_capable = any(surface.switch_source is not None for surface in self.surfaces)
        if self._graph_capable:
            graph_expand = self._graph_expand_tool()
            find_paths = self._find_paths_tool()
            toolset.add_function(
                graph_expand, takes_ctx=False, name=GRAPH_EXPAND, description=self._graph_expand_description(), retries=TOOL_MAX_RETRIES
            )
            toolset.add_function(
                find_paths, takes_ctx=False, name=FIND_PATHS, description=self._find_paths_description(), retries=TOOL_MAX_RETRIES
            )
            calls[GRAPH_EXPAND] = graph_expand
            calls[FIND_PATHS] = find_paths
        self._toolset = toolset
        self._calls = calls

    def get_toolset(self) -> AbstractToolset[AgentDepsT]:
        """The read tools for this request: dialed descriptions, and graph tools when earned.

        Returns:
            The toolset, prepared per request when dials or graph tools can vary.
        """
        if self.dials_source is None and not self._graph_capable:
            return self._toolset
        return self._toolset.prepared(self._dialed_defs)

    def _dials(self) -> ReadDials:
        """The modes in effect for the call being made."""
        return self.dials_source() if self.dials_source is not None else DEFAULT_DIALS

    async def _effective_hybrid(self, surface: ReadSurface) -> bool:
        """Whether this call runs the vector leg: the dial can force keyword, and an auto-mode switch must be on."""
        if not surface.hybrid or self._dials().search == "keyword":
            return False
        if surface.switch_source is None:
            return True
        switches = await surface.switch_source()
        return switches.embeddings

    async def _search_surface(
        self, surface: ReadSurface, query: str, k: int, *, since: date | None, until: date | None, facets: _Facets
    ) -> tuple[tuple[SearchHit, ...], bool]:
        """One surface's ranked hits, each with a qualified ref, and whether the vector leg ran."""
        hybrid = await self._effective_hybrid(surface)
        message = Search(query=query, k=k, hybrid=hybrid, collection=surface.collection, since=since, until=until, facets=dict(facets or {}))
        return await _surface_search(surface, message), hybrid

    async def _search_answer(
        self, hits: tuple[SearchHit, ...], *, query: str, surfaces: tuple[ReadSurface, ...], hybrid: bool, dated: bool, filtered: bool
    ) -> SearchResult:
        """One `search` answer: status, next move, the stamp when a single surface answered, and its map ref under leveled reads."""
        status = search_result_status(hits)
        named = surfaces[0].name if len(surfaces) == 1 else ", ".join(surface.name for surface in surfaces)
        sole = surfaces[0] if len(surfaces) == 1 else None
        stamp = await sole.stamp() if sole is not None and sole.stamp is not None else None
        return bound_search_result(
            hits,
            status=status,
            next_steps=search_next_steps(
                hits,
                query=query,
                knowledge_base=named,
                hybrid=hybrid,
                status=status,
                map_collection=None if sole is None or self._dials().fetch == "flat" else sole.collection,
                dated=dated,
                filtered=filtered,
            ),
            knowledge_base=stamp,
        )

    async def _dialed_defs(self, _ctx: RunContext[AgentDepsT], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        """This request's tool list: dialed descriptions, `graph_expand` and `find_paths` dropped unless earned."""
        dials = self._dials()
        graph_shown = await self._graph_earned() if self._graph_capable else False
        default_name = self._resolved_default_name()
        described: list[ToolDefinition] = []
        for tool_def in tool_defs:
            if tool_def.name in _GRAPH_TOOL_NAMES:
                if graph_shown:
                    described.append(tool_def)
                continue
            if tool_def.name == FETCH and dials.fetch == "flat":
                described.append(replace(tool_def, description=flat_fetch_description(self._sole_collection())))
            elif tool_def.name == SEARCH:
                hybrid = False if dials.search == "keyword" else None
                described.append(replace(tool_def, description=self._search_description(hybrid=hybrid, default_name=default_name)))
            else:
                described.append(tool_def)
        return described

    def _resolved_default_name(self) -> str | None:
        """This request's default surface name, or `None` when none is currently chosen."""
        named = self._host_default_name()
        return named if named is not None and any(surface.name == named for surface in self.surfaces) else None

    def get_instructions(self) -> Callable[[RunContext[AgentDepsT]], str]:
        """The instructions: what each knowledge base holds and how to retrieve from it well.

        The guidance is read when each request is built.

        Returns:
            A callable composing each knowledge base's domain sentence and guidance.
        """

        def compose(_ctx: RunContext[AgentDepsT]) -> str:
            if len(self.surfaces) == 1:
                surface = self.surfaces[0]
                return "\n\n".join(voice for voice in (surface.domain, _guidance(surface)) if voice)
            blocks = []
            for surface in self.surfaces:
                voice = surface.domain if (words := _guidance(surface)) is None else f"{surface.domain}\n{words}"
                blocks.append(f"`{surface.name}`: {voice}")
            return "## Your knowledge bases\n" + "\n\n".join(blocks)

        return compose

    def _search_tool(self) -> NativeTool:
        """The one `search`: a single knowledge base keeps the plain signature, several add `knowledge_base`."""
        by_name = {surface.name: surface for surface in self.surfaces}

        async def run_one(query: str, k: int, *, since: str | None, until: str | None, facets: _Facets) -> SearchResult:
            surface = self.surfaces[0]
            refuse_undeclared_facets(facets or {}, {surface.name: surface.facet_keys}, ModelRetry)
            first, last = _date_range(since, until)
            async with read_snapshot():
                hits, hybrid = await self._search_surface(surface, query, k, since=first, until=last, facets=facets)
                return await self._search_answer(
                    hits, query=query, surfaces=(surface,), hybrid=hybrid, dated=first is not None or last is not None, filtered=bool(facets)
                )

        async def run_many(
            query: str, k: int, knowledge_base: str | None, *, since: str | None, until: str | None, facets: _Facets
        ) -> SearchResult:
            # With cross_search off, _resolve_knowledge_base answers exactly one surface or raises, so this gathers over one.
            chosen = self._resolve_knowledge_base(knowledge_base, by_name)
            # A filter that passes is declared by every chosen surface, so each one applies all of it.
            refuse_undeclared_facets(facets or {}, {surface.name: surface.facet_keys for surface in chosen}, ModelRetry)
            first, last = _date_range(since, until)
            async with read_snapshot():
                ranked = await asyncio.gather(
                    *(self._search_surface(surface, query, k, since=first, until=last, facets=facets) for surface in chosen)
                )
                merged = _merged_by_rank([hits for hits, _ in ranked])
                # "hybrid" only when every surface reached ran its vector leg.
                return await self._search_answer(
                    tuple(merged[:k]),
                    query=query,
                    surfaces=chosen,
                    hybrid=all(leg for _, leg in ranked),
                    dated=first is not None or last is not None,
                    filtered=bool(facets),
                )

        if (resolve_k := self.k_source) is not None:
            return self._capped_search(resolve_k, run_one, run_many)
        return self._agent_k_search(run_one, run_many)

    def _capped_search(self, resolve_k: Callable[[], int], run_one: _RunOne, run_many: _RunMany) -> NativeTool:
        """The `search` signature under a host-owned cap: no `k` at all, so nothing downstream ignores one."""

        async def search_one_capped(
            query: str, *, since: Period | None = None, until: Period | None = None, facets: dict[str, list[str]] | None = None
        ) -> SearchResult:
            return await run_one(query, resolve_k(), since=since, until=until, facets=facets)

        async def search_named_capped(
            query: str, knowledge_base: str, *, since: Period | None = None, until: Period | None = None, facets: dict[str, list[str]] | None = None
        ) -> SearchResult:
            return await run_many(query, resolve_k(), knowledge_base, since=since, until=until, facets=facets)

        async def search_many_capped(
            query: str,
            knowledge_base: str | None = None,
            *,
            since: Period | None = None,
            until: Period | None = None,
            facets: dict[str, list[str]] | None = None,
        ) -> SearchResult:
            return await run_many(query, resolve_k(), knowledge_base, since=since, until=until, facets=facets)

        if len(self.surfaces) == 1:
            return search_one_capped
        return search_many_capped if self.cross_search else search_named_capped

    def _agent_k_search(self, run_one: _RunOne, run_many: _RunMany) -> NativeTool:
        """The `search` signature when the agent argues its own `k`."""

        async def search_one(
            query: str, k: int = 12, *, since: Period | None = None, until: Period | None = None, facets: dict[str, list[str]] | None = None
        ) -> SearchResult:
            return await run_one(query, k, since=since, until=until, facets=facets)

        async def search_named(
            query: str,
            knowledge_base: str,
            k: int = 12,
            *,
            since: Period | None = None,
            until: Period | None = None,
            facets: dict[str, list[str]] | None = None,
        ) -> SearchResult:
            return await run_many(query, k, knowledge_base, since=since, until=until, facets=facets)

        async def search_many(
            query: str,
            k: int = 12,
            knowledge_base: str | None = None,
            *,
            since: Period | None = None,
            until: Period | None = None,
            facets: dict[str, list[str]] | None = None,
        ) -> SearchResult:
            return await run_many(query, k, knowledge_base, since=since, until=until, facets=facets)

        if len(self.surfaces) == 1:
            return search_one
        return search_many if self.cross_search else search_named

    def _resolve_knowledge_base(self, knowledge_base: str | None, by_name: Mapping[str, ReadSurface]) -> tuple[ReadSurface, ...]:
        """Which surfaces one `search` call reaches, refusing with a `ModelRetry` toward the list of names."""
        if not self.cross_search:
            return (self._one_named(knowledge_base, by_name, unnamed=_NAME_TO_SEARCH),)
        if knowledge_base is None:
            return self._default_chosen(by_name)
        if knowledge_base == "all":
            return self.surfaces
        surface = by_name.get(knowledge_base)
        if surface is None:
            msg = (
                f"No knowledge base named {knowledge_base!r}. Your knowledge bases: {', '.join(by_name)}. "
                'Pass knowledge_base="all" to search every one, or omit `knowledge_base` to use the default.'
            )
            raise ModelRetry(msg)
        return (surface,)

    def _one_named(self, knowledge_base: str | None, by_name: Mapping[str, ReadSurface], *, unnamed: str) -> ReadSurface:
        """The single knowledge base a call names, refusing with the names anything that names none."""
        names = ", ".join(by_name)
        if knowledge_base is None or not knowledge_base.strip():
            msg = f"{unnamed} Your knowledge bases: {names}."
            raise ModelRetry(msg)
        if knowledge_base == "all":
            msg = f"Searching every knowledge base at once is off here. Name one of: {names}."
            raise ModelRetry(msg)
        surface = by_name.get(knowledge_base)
        if surface is None:
            msg = f"No knowledge base named {knowledge_base!r}. Your knowledge bases: {names}."
            raise ModelRetry(msg)
        return surface

    def _default_chosen(self, by_name: Mapping[str, ReadSurface]) -> tuple[ReadSurface, ...]:
        """Every surface an omitted `knowledge_base` searches: the host's default when it names a surface, else all."""
        named = self._host_default_name()
        surface = by_name.get(named) if named is not None else None
        return (surface,) if surface is not None else self.surfaces

    def _host_default_name(self) -> str | None:
        """The name `default_knowledge_base_source` answers now, or `None` while `cross_search` is off or no source is set."""
        if not self.cross_search or self.default_knowledge_base_source is None:
            return None
        return self.default_knowledge_base_source()

    def _graph_expand_tool(self) -> NativeTool:
        """The one `graph_expand`, with a `knowledge_base` argument over several surfaces."""

        async def graph_expand(
            seed_ids: list[str], hops: int = 2, knowledge_base: str | None = None, *, include_superseded: bool = False
        ) -> GraphResult:
            surface = await self._graph_target(knowledge_base)
            async with read_snapshot():
                result = await surface.handlers.graph_expand(
                    GraphExpand(seed_ids=tuple(seed_ids), hops=hops, include_superseded=include_superseded)
                )
                stamp = await surface.stamp() if surface.stamp is not None else None
            return routed_graph_result(
                result,
                knowledge_base=stamp,
                next_steps=graph_next_steps(
                    result.nodes,
                    result.edges,
                    truncated=result.window.truncated,
                    tools=_READ_TOOL_NAMES,
                    seed_ids=tuple(seed_ids),
                    hops=expansion_hops(hops),
                    reach=result.reach,
                ),
            )

        async def graph_expand_one(seed_ids: list[str], hops: int = 2, *, include_superseded: bool = False) -> GraphResult:
            return await graph_expand(seed_ids, hops, include_superseded=include_superseded)

        return graph_expand_one if len(self.surfaces) == 1 else graph_expand

    def _find_paths_tool(self) -> NativeTool:
        """The one `find_paths`, with a `knowledge_base` argument over several surfaces."""

        async def find_paths(
            from_id: str, to_id: str, knowledge_base: str | None = None, max_hops: int = 4, *, include_superseded: bool = False
        ) -> GraphPathsResult:
            surface = await self._graph_target(knowledge_base)
            async with read_snapshot():
                result = await surface.handlers.find_paths(
                    GraphPaths(from_id=from_id, to_id=to_id, max_hops=max_hops, include_superseded=include_superseded)
                )
                stamp = await surface.stamp() if surface.stamp is not None else None
            return routed_graph_paths(
                result,
                knowledge_base=stamp,
                next_steps=paths_next_steps(
                    result.paths,
                    from_id=from_id,
                    to_id=to_id,
                    truncated=result.window.truncated,
                    tools=_READ_TOOL_NAMES,
                    max_hops=route_hops(max_hops),
                    reach=result.reach,
                ),
            )

        async def find_paths_one(from_id: str, to_id: str, max_hops: int = 4, *, include_superseded: bool = False) -> GraphPathsResult:
            return await find_paths(from_id, to_id, max_hops=max_hops, include_superseded=include_superseded)

        return find_paths_one if len(self.surfaces) == 1 else find_paths

    async def _graph_target(self, knowledge_base: str | None) -> ReadSurface:
        """The surface a graph call routes to: the named earner, the host's default, or the sole earner."""
        if knowledge_base == "all":
            raise ModelRetry(await self._all_refusal())
        by_name = {surface.name: surface for surface in self.surfaces}
        if knowledge_base is not None:
            named = by_name.get(knowledge_base)
            if named is not None and await self._surface_earned_graph(named):
                return named
            raise ModelRetry(await self._no_earner_named(knowledge_base))
        default = await self._default_graph_target(by_name)
        if default is not None:
            return default
        earners = await self._earning_surfaces()
        if len(earners) == 1:
            return earners[0]
        if not earners:
            msg = "No knowledge graph is available here yet. Use `search` instead."
            raise ModelRetry(msg)
        names = ", ".join(surface.name for surface in earners)
        msg = f"Several knowledge bases have a graph available ({names}). Pass `knowledge_base` to choose one."
        raise ModelRetry(msg)

    async def _default_graph_target(self, by_name: Mapping[str, ReadSurface]) -> ReadSurface | None:
        """The host's default when it names a surface that currently earns a graph, else `None`."""
        named = self._host_default_name()
        surface = by_name.get(named) if named is not None else None
        if surface is not None and await self._surface_earned_graph(surface):
            return surface
        return None

    async def _no_earner_named(self, knowledge_base: str) -> str:
        """The refusal text for a `knowledge_base` name that is unknown or does not currently earn a graph."""
        earners = await self._earning_surfaces()
        if not earners:
            return f"No knowledge base named {knowledge_base!r} has an available knowledge graph. None does yet. Use `search` instead."
        names = ", ".join(surface.name for surface in earners)
        return f"No knowledge base named {knowledge_base!r} with an available knowledge graph. Available: {names}."

    async def _all_refusal(self) -> str:
        """The `knowledge_base="all"` refusal text for a graph read."""
        earners = await self._earning_surfaces()
        if not earners:
            return "Graph reads take one knowledge base at a time. None has an available graph yet. Use `search` instead."
        names = ", ".join(surface.name for surface in earners)
        return f"Graph reads take one knowledge base at a time. Pass `knowledge_base` to pick one of: {names}."

    async def _earning_surfaces(self) -> list[ReadSurface]:
        """Every surface that currently has its graph switch on, read fresh."""
        return [surface for surface in self.surfaces if await self._surface_earned_graph(surface)]

    async def _graph_earned(self) -> bool:
        """Whether any surface currently earns the graph tools."""
        for surface in self.surfaces:
            if await self._surface_earned_graph(surface):
                return True
        return False

    async def _surface_earned_graph(self, surface: ReadSurface) -> bool:
        """Whether `surface` currently has its stored graph switch on, `False` for a fixed-mode surface."""
        if surface.switch_source is None:
            return False
        switches = await surface.switch_source()
        return switches.graph

    def _fetch_tool(self) -> NativeTool:
        """The one `fetch`: leveled reads over the named knowledge base, or over every one of them."""

        async def fetch(unit_id: str = "") -> DisclosureEnvelope | FetchResult:
            if self._dials().fetch == "flat":
                return await self._flat(unit_id)
            if unit_id:
                return await self._open(unit_id)
            if len(self.surfaces) == 1:
                return await self._map(self.surfaces[0])
            return self._library_map()

        async def fetch_named(knowledge_base: str = "", unit_id: str = "") -> DisclosureEnvelope | FetchResult:
            surface = self._read_target(knowledge_base, unit_id)
            if self._dials().fetch == "flat":
                return await self._flat_in(surface, unit_id)
            return await self._open_in(surface, unit_id)

        return fetch if self.cross_search or len(self.surfaces) == 1 else fetch_named

    def _read_target(self, knowledge_base: str, unit_id: str) -> ReadSurface:
        """The one base a named read is answered by: the one named, or the one a ref names itself."""
        if not knowledge_base.strip() and (owner := _named_collection(unit_id)) is not None:
            return self._collection_surface(owner)
        return self._one_named(knowledge_base or None, {surface.name: surface for surface in self.surfaces}, unnamed=_NAME_TO_READ)

    async def _flat(self, unit_id: str) -> FetchResult:
        """One unit's full text under the flat dial, refusing a map request toward `search`."""
        if not unit_id or unit_id in {name for surface in self.surfaces for name in (surface.name, surface.collection)}:
            raise ModelRetry(no_map_refusal())
        if (owner := _named_collection(unit_id)) is not None:
            return await self._flat_in(self._collection_surface(owner), unit_id)
        refused: RefRefusal | None = None
        unknown = False
        chosen = self._default_chosen({candidate.name: candidate for candidate in self.surfaces})
        for surface in chosen:
            try:
                return await self._flat_in(surface, unit_id)
            except RefRefusal as refusal:
                refused = refusal
            except ModelRetry:
                unknown = True
        raise _fan_out_refusal(unit_id, refused, unknown=unknown, collection=_sole(chosen))

    async def _flat_in(self, surface: ReadSurface, unit_id: str) -> FetchResult:
        """One unit's full text, read from `surface` alone and stamped by it."""
        if not unit_id or unit_id in (surface.name, surface.collection):
            raise ModelRetry(no_map_refusal())
        try:
            resolved = parse_ref(unit_id, surface.collection)
        except BadRefError as exc:
            raise RefRefusal(str(exc)) from None
        if resolved.kind in {"collection", "group"}:
            raise ModelRetry(no_map_refusal())
        if resolved.collection != surface.collection:
            raise RefRefusal(str(WrongCollectionError(surface.collection, resolved.collection)))
        unit = await fetch_unit(surface.handlers, resolved.unit_id)
        if unit is None:
            raise ModelRetry(no_unit_refusal(resolved.unit_id, surface.collection, named=f" in {surface.name!r}"))
        stamp = await surface.stamp() if surface.stamp is not None else None
        return unit.model_copy(update={"ref": qualify_ref(f"unit:{unit.id}", surface.collection), "knowledge_base": stamp})

    async def _open_in(self, surface: ReadSurface, unit_id: str) -> FetchEnvelope | FetchUnit:
        """One ref answered by one knowledge base alone, its map when the id is empty or the base's own name."""
        if not unit_id or unit_id in (surface.name, surface.collection):
            return await self._map(surface)
        return await self._reads[surface.collection](unit_id)

    def _collection_surface(self, collection: str) -> ReadSurface:
        """The knowledge base serving `collection`, refusing a collection no surface serves."""
        for surface in self.surfaces:
            if surface.collection == collection:
                return surface
        choices = ", ".join(surface.collection for surface in self.surfaces)
        msg = f"No knowledge base here for collection {collection!r}. Your knowledge bases: {choices}. Use `search` to find a ref."
        raise ModelRetry(msg)

    async def _open(self, unit_id: str) -> DisclosureEnvelope | FetchResult:
        """One held ref, answered by the base it names, or by the first that recognizes a bare id."""
        by_name = {surface.name: surface for surface in self.surfaces}
        if (named := by_name.get(unit_id)) is not None:
            return await self._map(named)
        if (owner := _named_collection(unit_id)) is not None:
            return await self._open_in(self._collection_surface(owner), unit_id)
        refused: RefRefusal | None = None
        unknown = False
        chosen = self._default_chosen(by_name)
        for surface in chosen:
            try:
                return await self._open_in(surface, unit_id)
            except RefRefusal as refusal:
                refused = refusal
            except ModelRetry:
                unknown = True
        raise _fan_out_refusal(unit_id, refused, unknown=unknown, collection=_sole(chosen))

    async def _map(self, surface: ReadSurface) -> FetchEnvelope | FetchUnit:
        """The knowledge base map, as `surface.envelope` renders it, under that base's own stamp."""
        return await self._reads[surface.collection](f"collection:{surface.collection}")

    def _library_map(self) -> DisclosureEnvelope:
        """The no-argument read with several surfaces: the knowledge bases themselves, as a level."""
        children = tuple(
            ChildSummary(ref=f"collection:{surface.collection}", kind="collection", title=surface.name, summary=surface.domain)
            for surface in self.surfaces
        )
        return DisclosureEnvelope(
            ref="library",
            kind="library",
            title="Your knowledge bases",
            summary=f"{len(self.surfaces)} knowledge bases. Open one's map by its ref.",
            mode="strict",
            children=children,
        )

    def _search_description(self, *, hybrid: bool | None = None, default_name: str | None = None) -> str:
        """The `search` description for one knowledge base or several, without `k` under a host-owned cap."""
        leg = (self.surfaces[0].hybrid if len(self.surfaces) == 1 else any(surface.hybrid for surface in self.surfaces)) if hybrid is None else hybrid
        text = (
            search_description(hybrid=leg, can_sql=False, facet_keys=self.surfaces[0].facet_keys, takes_collection=False)
            if len(self.surfaces) == 1
            else self._library_search_description(hybrid=leg, default_name=default_name)
        )
        return elide_k(text) if self.k_source is not None else text

    def _library_search_description(self, *, hybrid: bool, default_name: str | None = None) -> str:
        """The `search` description with several knowledge bases: the same text, plus `knowledge_base`."""
        ranking = "semantic + keyword ranking, fused" if hybrid else "keyword ranking"
        return (
            f"Search your knowledge bases ({ranking}): returns the top-`k` "
            "ranked hits (a page, a section within one, or a smaller unit) as a list, each with its "
            "`id`, `hit_level`, `page_id`/`page_title`, a `snippet`, and a relevance `score`.\n\n"
            f"`query` is the search text. `k` caps the hits (default 12). {DATE_RANGE_CLAUSE} "
            f"{declared_facets_clause({surface.name: surface.facet_keys for surface in self.surfaces}, several_per_call=self.cross_search)} "
            f"{self._knowledge_base_clause(default_name=default_name)}\n"
            f"{self._knowledge_base_listing()}\n\n"
            "Start here to find candidate sources, then `fetch` a hit's id."
        )

    def _knowledge_base_clause(self, *, default_name: str | None = None) -> str:
        """The sentence `search`'s description spends on `knowledge_base`, ending at the colon before the listing."""
        if not self.cross_search:
            return "`knowledge_base` names the one knowledge base to search and is required, one base per call:"
        if default_name is not None:
            omitted = f"omitting it searches `{default_name}`"
        elif self.default_knowledge_base_source is not None:
            omitted = "omitting it searches the base chosen for you"
        else:
            omitted = "omitting it searches them all together"
        return f'`knowledge_base` limits the search to one knowledge base, {omitted}. Pass `knowledge_base="all"` to search every one at once:'

    def _graph_expand_description(self) -> str:
        """The `graph_expand` description: the tuned flat text, or the library variant naming `knowledge_base`."""
        if len(self.surfaces) == 1:
            return GRAPH_EXPAND_DESCRIPTION
        return (
            "Traverse a knowledge base's graph outward from one or more seed pages and return the "
            "reached neighbourhood as `{nodes, edges}`. `seed_ids` are the starting page ids (ground "
            "them with a prior `search`): passing SEVERAL seeds in one call maps their combined "
            "neighbourhood, the efficient way to see how two things each relate to a shared family "
            "or component. `hops` is the traversal depth (default 2). Each edge is a typed "
            "relationship and carries its `provenance_claim` (the unit id that asserts it, when "
            "known).\n\n"
            f"{self._graph_knowledge_base_clause(verb='walk')}\n\n"
            "Use it to discover what a page connects to (sources it references, depends on, or "
            "relates to) that a flat search may miss."
        )

    def _find_paths_description(self) -> str:
        """The `find_paths` description: the tuned flat text, or the library variant naming `knowledge_base`."""
        if len(self.surfaces) == 1:
            return FIND_PATHS_DESCRIPTION
        return (
            "Find the connecting paths between two nodes in a knowledge base's graph and return "
            "them ranked as a list. Each path is its ordered `nodes`, the typed `edges` between "
            "them (each with its `provenance_claim`), and a `score`. `from_id` / `to_id` are the "
            "endpoint ids (ground them with `search` first).\n\n"
            f"{self._graph_knowledge_base_clause(verb='search')}\n\n"
            "Use it to explain HOW two items relate (the chain of relationships linking them) "
            "when the question is about a connection or influence. Paths follow edge DIRECTION, so "
            "an empty result means no directed chain from `from_id` to `to_id` is recorded, not "
            "that the two are unrelated: try the reverse direction, or `graph_expand` with both ids "
            "as seeds to surface a shared neighbour."
        )

    def _graph_knowledge_base_clause(self, *, verb: str) -> str:
        """The `knowledge_base` clause both graph tool descriptions state, plus the knowledge base listing."""
        omitted = (
            "omitting it routes to the base chosen for you when it currently has one, else the sole "
            "knowledge base that does"
            if self.cross_search and self.default_knowledge_base_source is not None
            else "omitting it routes to the sole knowledge base that currently has one"
        )
        return (
            f'`knowledge_base` selects which knowledge base to {verb}. {omitted}. `knowledge_base="all"` is not '
            "accepted here: a graph read takes one base at a time:\n"
            f"{self._knowledge_base_listing()}"
        )

    def _knowledge_base_listing(self) -> str:
        """The knowledge-base bullet list a library-variant description names, one line per surface."""
        return "\n".join(f"- '{surface.name}': {surface.domain.split('.')[0].lower()}" for surface in self.surfaces)

    def _sole_collection(self) -> str | None:
        """The one surface's collection, or `None` when this capability holds several."""
        return _sole(self.surfaces)

    def _fetch_description(self) -> str:
        """The `fetch` description: the knowledge base's own text, or the variant for several."""
        if len(self.surfaces) == 1:
            surface = self.surfaces[0]
            return fetch_description(surface.collection, mode=surface.envelope.mode)
        description = library_fetch_description([surface.name for surface in self.surfaces], cross_search=self.cross_search)
        return description.replace("and is required", "and is required for an unqualified id").replace(
            "Ids from `search` hits work here.",
            "A `ref` from a hit or a listing already names its base, so pass one back exactly as returned and it needs no `knowledge_base`.",
        )

    async def list_tools(self) -> list[mcp_types.Tool]:
        """The tools this capability offers right now, in MCP's `Tool` shape.

        `graph_expand` and `find_paths` are listed only while some surface currently earns them.

        Returns:
            One MCP `Tool` per tool currently offered.
        """
        graph_shown = await self._graph_earned() if self._graph_capable else False
        return [
            mcp_types.Tool(name=tool.name, description=tool.description, inputSchema=dict(tool.tool_def.parameters_json_schema))
            for tool in self._toolset.tools.values()
            if tool.name not in _GRAPH_TOOL_NAMES or graph_shown
        ]


def _named_collection(ref: str) -> str | None:
    """The collection `ref` names outright, or `None` when it carries none."""
    try:
        resolved = parse_ref(ref, _UNNAMED)
    except BadRefError:
        return None
    return None if resolved.collection == _UNNAMED else resolved.collection


def _sole(surfaces: Sequence[ReadSurface]) -> str | None:
    """The collection of the one surface in `surfaces`, or `None` when there are several."""
    return surfaces[0].collection if len(surfaces) == 1 else None


def _fan_out_refusal(unit_id: str, refused: RefRefusal | None, *, unknown: bool, collection: str | None) -> ModelRetry:
    """The refusal for a read no knowledge base answered: not-found naming the forms in `collection`, unless every base refused the ref itself."""
    if refused is not None and not unknown:
        return refused
    return ModelRetry(no_unit_refusal(unit_id, collection))


def _date_range(since: str | None, until: str | None) -> tuple[date | None, date | None]:
    """The first day `since` names and the last day `until` names, `None` for a bound left open."""
    return (None if since is None else period_start(since), None if until is None else period_end(until))


def _merged_by_rank(lists: Sequence[tuple[SearchHit, ...]]) -> list[SearchHit]:
    """Hits of several knowledge bases in one order: by re-rank probability when every hit has one, else rank by rank."""
    placed = [(position, base, hit) for base, hits in enumerate(lists) for position, hit in enumerate(hits)]
    if placed and all(hit.rerank_score is not None for _position, _base, hit in placed):
        placed.sort(key=lambda entry: (-(entry[2].rerank_score or 0.0), entry[0], entry[1]))
    else:
        placed.sort(key=lambda entry: (entry[0], entry[1]))
    return [hit for _position, _base, hit in placed]


async def _surface_search(surface: ReadSurface, message: Search) -> tuple[SearchHit, ...]:
    """One surface's ranked hits for `message`, each stamped with the legs that selected it and the surface's name."""
    result = await surface.handlers.search(message)
    return search_hits(result.rows, collection=surface.collection, knowledge_base=surface.name, hybrid=message.hybrid)


def _guidance(surface: ReadSurface) -> str | None:
    """A surface's retrieval guidance for the request being built, or `None` if it has none."""
    return surface.guidance() if surface.guidance is not None else None
