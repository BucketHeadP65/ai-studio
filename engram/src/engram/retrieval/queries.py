r"""Read-side query handlers, one handler class per retrieval query message.

Each handler takes its `SqlGateway` (and, for search, its `BatchEmbedder`) in its constructor and
turns one query message into one call against the read-only `api` surface. `StatusHandler`,
`KnowledgeBasesHandler` and `KnowledgeBaseValidationHandler` read `kb.*` tables, so they take the
admin gateway.
"""

from __future__ import annotations

import difflib
import json

from datetime import datetime
from typing import TYPE_CHECKING

from core.ai.retrieval.contracts import (
    FETCH,
    GRAPH_EXPAND,
    GRAPH_EXPAND_HOP_LIMIT,
    GRAPH_PATHS_HOP_LIMIT,
    GRAPH_REACH_NODE_LIMIT,
    GRAPH_TOP_K_CAP,
    SEARCH,
    TOOL_NAMES,
    GraphPathsResult,
    GraphReach,
    KnowledgeBaseStamp,
    ResultWindow,
)
from pydantic import BaseModel, ConfigDict

from engram.indexing.contracts import Severity

from .budget import clip_text, fits_budget, longest_fitting, with_fallback_steps
from .domain import GraphEdge, GraphNode, GraphPath, GraphResult, KeywordOnlyResult, NextSteps, SqlResult, StatusReport
from .envelope import qualify_ref
from .exceptions import DescribeSchemaRefusedError, QueryNotEmbeddedError
from .query_embedding import EMBED_DEADLINE_SECONDS, embed_within

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import Protocol

    from core.ai.embedding import BatchEmbedder

    from .domain import (
        ColumnInfo,
        DescribeSchema,
        Fetch,
        Graph,
        GraphExpand,
        GraphPaths,
        LookupValue,
        ObjectInfo,
        Row,
        SchemaDescription,
        Search,
        SqlGateway,
        Status,
    )

    class SchemaGateway(Protocol):
        """The part of `SqlGateway` `DescribeSchemaHandler` needs, introspecting the `api` surface."""

        async def api_objects(self, names: tuple[str, ...] = ()) -> SchemaDescription:
            """Describe every `api` object, or only `names` when given.

            Args:
                names: Bare, unqualified object names to describe, or empty for every object.

            Returns:
                The description of the selected objects.
            """
            ...

    class PageTypeView(Protocol):
        """The part of a page type `DescribeSchemaHandler` needs: its page type, id prefix, and whether it has a decision."""

        @property
        def page_type(self) -> str:
            """The page-type slug."""
            ...

        @property
        def id_prefix(self) -> str:
            """This page_type's id prefix, or `""` when its ids carry no fixed prefix."""
            ...

        @property
        def decision_declared(self) -> bool:
            """Whether this page_type declares a decision block (an open status and its outcomes)."""
            ...

    class KnowledgeBaseSchemaView(Protocol):
        """The part of a knowledge base schema `DescribeSchemaHandler` needs: its identity and its declared page types."""

        @property
        def page_types(self) -> Sequence[PageTypeView]:
            """This knowledge base's declared page types, in declaration order."""
            ...

        @property
        def slug(self) -> str:
            """This knowledge base's slug."""
            ...

# The kb.* tables Status reports row counts for (a fixed allowlist, never user input).
_STATUS_TABLES: tuple[str, ...] = ("pages", "sections", "claims", "edges", "sources_raw", "raw_chunks", "vocab")

# The `api.search` call behind the structured search tool. `:qvec` is a halfvec text literal cast
# through `text`, and NULL runs the BM25-only leg. `:query_model` names the model that made `:qvec`, so
# a collection indexed under another model gets no semantic leg. `include_meta => false` leaves every
# `meta.*` page out before `k` is applied. It returns every column `api.search` projects, including the
# per-leg ranks and the snippet, and applies no threshold.
SEARCH_SQL = (
    "SELECT * FROM api.search("
    "query => :q, k => :k, query_vec => CAST(:qvec AS text)::halfvec, "
    "level => :level, facets => CAST(:facets AS jsonb), collection => :collection, "
    "since => CAST(:since AS date), until => CAST(:until AS date), query_model => CAST(:query_model AS text), "
    "include_meta => false)"
)

# The cap on hits per document `api.search` applies to one knowledge base, as its search tuning sets it, 0 for every hit kept.
HITS_PER_DOCUMENT_SQL = "SELECT api.search_hits_per_document(:collection) AS hits_per_document"

# The `api.fetch` call behind the structured fetch tool. A `meta.*` id returns no unit. An id is
# unique only within its collection, so `:collection` is always bound.
FETCH_SQL = "SELECT * FROM api.fetch(:collection, :unit_id) WHERE id NOT LIKE 'meta.%'"
_LOOKUP_SQL = "SELECT * FROM api.lookup_value(:collection, :facet, :term)"

# The whole graph of one collection. Nodes are page cards, filtered by `:page_type` when it is not
# NULL. Edges keep only those whose two endpoints are both in the node set, filtered by `:rel` when it
# is not NULL. A nullable bind is cast to `text` so asyncpg can infer its type.
_GRAPH_CAP = 5000
_GRAPH_NODES_SQL = """
SELECT id, type, title, status, superseded
FROM api.cards
WHERE src_collection = :collection
  AND (CAST(:page_type AS text) IS NULL OR type = CAST(:page_type AS text))
"""
_GRAPH_EDGES_SQL = """
SELECT src_id, dst_id, rel, provenance_claim
FROM api.edges
WHERE src_collection = :collection
  AND dst_collection = :collection
  AND (CAST(:rel AS text) IS NULL OR rel = CAST(:rel AS text))
  AND src_id IN (
    SELECT id FROM api.cards
    WHERE src_collection = :collection AND (CAST(:page_type AS text) IS NULL OR type = CAST(:page_type AS text))
  )
  AND dst_id IN (
    SELECT id FROM api.cards
    WHERE src_collection = :collection AND (CAST(:page_type AS text) IS NULL OR type = CAST(:page_type AS text))
  )
"""

# The multi-hop traversal calls, each scoped to one collection. `:rel_types` is a `text[]`, and NULL
# means every relation. `api.graph_expand` returns one row per reached node and reaching edge, so a
# node reached by two assertions comes back twice and a seed comes back once with NULL edge columns.
# `api.graph_paths` returns one node per row, tagged with its `path_rank` and the path `score`. Both
# return one item past the top-k clamp, so a short answer can be reported as cut. With
# `:include_superseded` false the traversal skips a superseded node at every hop.
GRAPH_EXPAND_SQL = "SELECT * FROM api.graph_expand(:collection, :seed_ids, :hops, CAST(:rel_types AS text[]), :k, :include_superseded)"
GRAPH_PATHS_SQL = (
    "SELECT * FROM api.graph_paths(:collection, :from_id, :to_id, :max_hops, CAST(:rel_types AS text[]), :k, :include_superseded)"
)

# How far a seed set reaches with no hop limit, with the same filters as the walks: one row, or none when the collection
# is not visible to the session. `:target_id` is the route's end, or NULL.
GRAPH_REACH_SQL = (
    "SELECT * FROM api.graph_reach(:collection, :seed_ids, CAST(:rel_types AS text[]), CAST(:target_id AS text), :include_superseded)"
)

# The most rows one traversal read may bring back. A node reached by several assertions takes several
# rows, so `k` is applied to the nodes and never to the rows.
_GRAPH_ROW_CAP = 5_000

GRAPH_RESULT_CHARS = 16_000
"""The longest complete serialized graph answer a traversal tool returns, in characters.

It counts the whole answer: nodes, edges, refs, titles, window, next steps and stamp.
"""

_GRAPH_TRUNCATION_REASON = (
    "Nodes beyond the ones returned were left out to keep this answer inside its size budget. Expand again with fewer "
    "hops, a rel_types filter, or a smaller k to see a narrower neighbourhood in full."
)
"""Why an expansion's window is short: what was cut, and the narrower call that avoids the cut."""

_PATHS_TRUNCATION_REASON = (
    "Routes beyond the ones returned were left out to keep this answer inside its size budget. Search again with a "
    "smaller max_hops, a rel_types filter, or a smaller k to see fewer routes in full."
)
"""Why a route list is short: what was cut, and the narrower call that avoids the cut."""

_GRAPH_REFUSAL_REASON = (
    "The first node's id and ref alone exceed this answer's size budget, so no node could be returned intact."
)
"""Why a refused expansion carries nothing: a node's id and ref are never shortened."""

_PATHS_REFUSAL_REASON = (
    "The first route's node ids and refs alone exceed this answer's size budget, so no route could be returned intact."
)
"""Why a refused route list carries nothing: a node's id and ref are never shortened."""

_GRAPH_REFUSAL_STEPS = NextSteps(
    summary="No node fits this answer's size budget, so none was returned.",
    options=("Expand again from a single seed with hops=1: a smaller neighbourhood reaches fewer nodes.",),
)
"""The fixed next move a budget refusal offers. It never repeats the identity that overflowed."""

_PATHS_REFUSAL_STEPS = NextSteps(
    summary="No route fits this answer's size budget, so none was returned.",
    options=("Search again with a smaller max_hops: a shorter route carries fewer nodes.",),
)
"""The fixed next move a refused route list offers. It never repeats the identity that overflowed."""

_MINIMAL_GRAPH_STEPS = NextSteps(
    summary="This answer's own next-move block did not fit its size budget, so this short one replaced it.",
    options=("Traverse again from a single seed with hops=1.",),
)
"""The next move that replaces a whole next-move block that did not fit the size budget."""

_ASSERTION_CLAUSE = "Every relation is an authored assertion: fetch its provenance_claim before relying on it."
"""What a returned edge is, said on every answer that carries one."""

# Underscore names for the four public SQL templates above.
_SEARCH_SQL = SEARCH_SQL
_FETCH_SQL = FETCH_SQL
_GRAPH_EXPAND_SQL = GRAPH_EXPAND_SQL
_GRAPH_PATHS_SQL = GRAPH_PATHS_SQL


class SearchHandler:
    """Handles `Search`, embed (when hybrid) then run `api.search`.

    With `hybrid=False` the vector is NULL, so every row carries a `lexical_rank` and no `vector_rank`.
    A hybrid search names the embedder's `model_id` as the query's model, and an empty one names none.
    A hybrid search raises what its embedding raises, unless it sets `keyword_fallback`: it then runs keyword only
    when the embedding fails or runs past the deadline, and answers a `KeywordOnlyResult` naming why.
    Rows come back exactly as `api.search` ranked them.
    """

    def __init__(self, gateway: SqlGateway, embedder: BatchEmbedder, *, embed_deadline: float = EMBED_DEADLINE_SECONDS) -> None:
        """Create the handler.

        Args:
            gateway: The read-only SQL gateway.
            embedder: The embedder the query is embedded with when hybrid.
            embed_deadline: The longest a `keyword_fallback` search's embedding may take, in seconds, before it runs keyword only.
        """
        self._gateway = gateway
        self._embedder = embedder
        self._embed_deadline = embed_deadline

    async def __call__(self, query: Search) -> SqlResult:
        """Embed the query once (when hybrid), then run `api.search` with the row cap = `k`.

        Args:
            query: The search to run.

        Returns:
            The ranked hits, as a `KeywordOnlyResult` when a `keyword_fallback` search could not embed its query.
        """
        vector, skipped = await self._query_vector(query)
        params = {
            "query_model": (self._embedder.model_id or None) if vector is not None else None,
            "q": query.query,
            "k": query.k,
            "qvec": None if vector is None else halfvec_literal(vector),
            "level": query.level,
            "facets": json.dumps(query.facets),
            "collection": query.collection,
            "since": query.since,
            "until": query.until,
        }
        result = await self._gateway.rows(SEARCH_SQL, params, cap=query.k)
        if skipped is None:
            return result
        return KeywordOnlyResult.model_validate({**dict(result), "reason": skipped})

    async def hits_per_document(self, collection: str) -> int:
        """Read the most hits one document of a knowledge base keeps in this handler's search, as its search tuning sets it.

        Args:
            collection: The knowledge base whose cap is read, by its collection.

        Returns:
            The cap, 0 when the search keeps every hit or the database answers no number.
        """
        result = await self._gateway.rows(HITS_PER_DOCUMENT_SQL, {"collection": collection}, cap=1)
        value = result.rows[0].get("hits_per_document") if result.rows else None
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    async def _query_vector(self, query: Search) -> tuple[list[float] | None, str | None]:
        """The query's vector, or none and why it was skipped when a `keyword_fallback` search could not embed it."""
        if not query.hybrid:
            return None, None
        if not query.keyword_fallback:
            return await self._embed_once(query.query), None
        try:
            return await embed_within(lambda: self._embed_once(query.query), deadline=self._embed_deadline), None
        except QueryNotEmbeddedError as exc:
            return None, exc.reason

    async def _embed_once(self, query: str) -> list[float]:
        """Embed the single query string (one `embed_batch` call, one returned vector)."""
        embeddings = await self._embedder.embed_batch([query])
        return embeddings.vectors[0]


class FetchHandler:
    """Handles `Fetch`, `api.fetch(:collection, :unit_id)`."""

    def __init__(self, gateway: SqlGateway, collection: str) -> None:
        """Create the handler.

        Args:
            gateway: The read-only SQL gateway.
            collection: The collection every fetch is scoped to.
        """
        self._gateway = gateway
        self._collection = collection

    async def __call__(self, query: Fetch) -> SqlResult:
        """Fetch one unit's full text by id, scoped to this handler's served collection.

        Args:
            query: The fetch, naming the unit id.

        Returns:
            The unit's row.
        """
        return await self._gateway.rows(FETCH_SQL, {"collection": self._collection, "unit_id": query.id})


class LookupValueHandler:
    """Handles `LookupValue`, `api.lookup_value(:collection, :facet, :term)`."""

    def __init__(self, gateway: SqlGateway, collection: str) -> None:
        """Create the handler.

        Args:
            gateway: The read-only SQL gateway.
            collection: The collection every lookup is scoped to.
        """
        self._gateway = gateway
        self._collection = collection

    async def __call__(self, query: LookupValue) -> SqlResult:
        """List a facet's allowed values with their definitions and page counts, within this handler's collection.

        Args:
            query: The lookup, naming the facet and the term to match.

        Returns:
            One row for each matching value.
        """
        return await self._gateway.rows(_LOOKUP_SQL, {"collection": self._collection, "facet": query.facet, "term": query.term})


SCHEMA_OBJECT_LIMIT = 8
"""How many objects one `describe_schema(objects=...)` call may name at a time."""

SCHEMA_RESULT_CHARS = 12_000
"""The longest complete `describe_schema` answer, in characters, for the catalog and a detail render alike."""

_SCHEMA_PURPOSE_CHARS = 300
"""How long a catalog line's one-line purpose may run before it is cut."""

_SCHEMA_FIELD_CHARS = 4_000
"""How long one object's comment, or one column's comment, may run in a detail render before it is cut."""

DESCRIBE_SCHEMA_CATALOG_INSTRUCTION = "Call describe_schema(objects=['api.search']) for columns and arguments."
"""The exact next-move sentence every catalog answer ends with."""

_SCHEMA_FIELD_CLIPPED_NOTE = (
    "One or more comments were shortened to fit this answer's size budget. Request fewer objects at once for full text."
)
"""Appended whenever a comment ran past `_SCHEMA_PURPOSE_CHARS` / `_SCHEMA_FIELD_CHARS` and was cut."""

_SCHEMA_OVERSIZE_NOTE = (
    "\n\n[Cut to fit this answer's size budget. Request fewer objects, or call describe_schema() for the catalog.]"
)
"""The last-resort narrowing instruction appended when even the shortened render still overruns the budget."""


class DescribeSchemaHandler:
    """Handles `DescribeSchema`: a compact catalog, or named-object detail, identified by the given schemas.

    With no `objects` named, the answer is every `api` object's name, kind and one-line purpose, in
    the gateway's own order, plus the instruction for asking for detail. Naming `objects` (bare or
    `api.`-qualified, at most `SCHEMA_OBJECT_LIMIT`) answers the full comment and typed columns of
    just those. An unknown name, or more names than the limit, raises `DescribeSchemaRefusedError`.

    The database's comments always render unchanged.

    * With one schema the answer opens with a `knowledge_base {slug}` line, plus its root when one is given.
    * With several schemas the answer is followed by one block per schema in the order given: its slug,
      page types, two example ids, and decision page types.
    """

    def __init__(self, gateway: SchemaGateway, specs: Sequence[KnowledgeBaseSchemaView] = (), *, root: Path | None = None) -> None:
        """Create the handler.

        Args:
            gateway: The gateway that introspects the `api` surface.
            specs: The schemas of the knowledge bases this answer speaks for.
            root: The knowledge base's root folder, named in the identity line when there is one schema.
        """
        self._gateway = gateway
        self._specs = tuple(specs)
        self._root = root

    async def __call__(self, query: DescribeSchema) -> str:
        """The catalog (no `objects` named) or named-object detail, identified, within its size budget.

        Args:
            query: The request, naming the objects to detail or none for the catalog.

        Returns:
            The rendered text.

        Raises:
            DescribeSchemaRefusedError: When the request names too many objects or an unknown one.
        """
        body = await self._catalog() if not query.objects else await self._detail(query.objects)
        return _bounded_schema_text(self._identified(body))

    async def _catalog(self) -> str:
        """Every object's name, kind and one-line purpose, in the gateway's own order."""
        description = await self._gateway.api_objects()
        return _render_catalog(description)

    async def _detail(self, requested: tuple[str, ...]) -> str:
        """The full comment + typed-column block for `requested`, refusing an over-limit or unknown ask."""
        if len(requested) > SCHEMA_OBJECT_LIMIT:
            raise DescribeSchemaRefusedError(_too_many_objects_message(len(requested)))
        normalized = _normalize_object_names(requested)
        bare = tuple(name.removeprefix("api.") for name in normalized)
        filtered = await self._gateway.api_objects(names=bare)
        found = {obj.name for obj in filtered.objects}
        missing = [name for name in normalized if name not in found]
        if missing:
            catalog = await self._gateway.api_objects()
            valid = tuple(obj.name for obj in catalog.objects)
            raise DescribeSchemaRefusedError(_unknown_object_message(missing, valid))
        return _render_schema(filtered)

    def _identified(self, body: str) -> str:
        """`body`, opened with the knowledge base identity line, or followed by its grounding blocks."""
        if len(self._specs) == 1:
            solo = self._specs[0]
            where = f" at {self._root}" if self._root is not None else ""
            return f"knowledge_base {solo.slug}{where}\n{body}"
        if not self._specs:
            return body
        blocks = "\n\n".join(_grounding_block(spec) for spec in self._specs)
        return f"{body}\n\n{blocks}"


def _too_many_objects_message(requested: int) -> str:
    """The one-sentence refusal for a `describe_schema(objects=...)` ask over `SCHEMA_OBJECT_LIMIT`."""
    return f"describe_schema(objects=...) takes at most {SCHEMA_OBJECT_LIMIT} names at a time, not {requested}."


def _unknown_object_message(missing: Sequence[str], valid: Sequence[str]) -> str:
    """The refusal for `missing` names, each with its close matches (when any) plus the full valid list."""
    named = []
    for name in missing:
        close = difflib.get_close_matches(name, valid, n=3, cutoff=0.5)
        named.append(f"{name!r} (close: {', '.join(close)})" if close else repr(name))
    return f"Unknown object name(s): {', '.join(named)}. Valid names: {', '.join(valid)}."


def _normalize_object_names(requested: tuple[str, ...]) -> tuple[str, ...]:
    """`requested`, bare names normalized to `api.<name>`, de-duplicated, first-seen order preserved."""
    normalized = (name if name.startswith("api.") else f"api.{name}" for name in (item.strip() for item in requested))
    return tuple(dict.fromkeys(normalized))


def _bounded_schema_text(text: str, *, max_chars: int = SCHEMA_RESULT_CHARS) -> str:
    """`text`, cut with a narrowing instruction when it still runs past `max_chars`."""
    if len(text) <= max_chars:
        return text
    room = max(max_chars - len(_SCHEMA_OVERSIZE_NOTE), 0)
    return text[:room] + _SCHEMA_OVERSIZE_NOTE


class GraphHandler:
    """Handles `Graph`, the whole graph of one collection from `api.cards` and `api.edges`.

    The nodes are narrowed by `page_type` and the edges by `rel`, and an edge is kept only when both
    its endpoints are nodes. `query.collection`, when set, overrides the handler's collection for that
    one call.
    """

    def __init__(self, gateway: SqlGateway, collection: str) -> None:
        """Create the handler.

        Args:
            gateway: The read-only SQL gateway.
            collection: The collection a query without its own collection is scoped to.
        """
        self._gateway = gateway
        self._collection = collection

    async def __call__(self, query: Graph) -> GraphResult:
        """Read the filtered node set and the edges between its nodes.

        Args:
            query: The graph request, with its optional collection, page type and relation filters.

        Returns:
            The nodes and the edges between them.
        """
        collection = query.collection if query.collection is not None else self._collection
        params = {"collection": collection, "page_type": query.page_type, "rel": query.rel}
        nodes = await self._gateway.rows(_GRAPH_NODES_SQL, params, cap=_GRAPH_CAP)
        edges = await self._gateway.rows(_GRAPH_EDGES_SQL, params, cap=_GRAPH_CAP)
        return GraphResult(
            nodes=tuple(_graph_node(row, collection) for row in nodes.rows),
            edges=tuple(_graph_edge(row, collection) for row in edges.rows),
        )


class GraphExpandHandler:
    """Handles `GraphExpand`, the seeds and the nodes reached from them by `api.graph_expand`, in one collection.

    `k` caps the nodes, never the edges, so a node reached by two assertions keeps both. When more
    nodes exist than `k`, the answer says it was cut and leaves the remainder uncounted. The answer is
    kept within `GRAPH_RESULT_CHARS`.
    """

    def __init__(self, gateway: SqlGateway, collection: str) -> None:
        """Create the handler.

        Args:
            gateway: The read-only SQL gateway.
            collection: The collection every traversal is scoped to.
        """
        self._gateway = gateway
        self._collection = collection

    async def __call__(self, query: GraphExpand, *, tools: frozenset[str] = TOOL_NAMES) -> GraphResult:
        """Run `api.graph_expand` and return the bounded node set and the edges between its nodes.

        Args:
            query: The expansion to run.
            tools: The tool names the caller registered. The next move names only these.

        Returns:
            The bounded subgraph with its next move.
        """
        params = {
            "collection": self._collection,
            "seed_ids": list(query.seed_ids),
            "hops": query.hops,
            "rel_types": _named_relations(query.rel_types),
            "k": query.k,
            "include_superseded": query.include_superseded,
        }
        result = await self._gateway.rows(GRAPH_EXPAND_SQL, params, cap=_GRAPH_ROW_CAP)
        reached = _unique_nodes(result.rows, self._collection)
        kept = reached[: _logical_cap(query.k)]
        edges = _edges_between(_reaching_edges(result.rows, self._collection), kept)
        more = len(reached) > len(kept)
        reach = None if more or not kept else await self._reach(query)
        hops = expansion_hops(query.hops)
        return bound_graph_result(
            kept,
            edges,
            knowledge_base=None,
            next_steps=graph_next_steps(kept, edges, truncated=more, tools=tools, seed_ids=query.seed_ids, hops=hops, reach=reach),
            omitted_items=None if more else 0,
            reach=reach,
        )

    async def _reach(self, query: GraphExpand) -> GraphReach | None:
        """How far the seeds reach with no hop limit, under the expansion's own filters."""
        params = {
            "collection": self._collection,
            "seed_ids": list(query.seed_ids),
            "rel_types": _named_relations(query.rel_types),
            "target_id": None,
            "include_superseded": query.include_superseded,
        }
        return _reach_of(await self._gateway.rows(GRAPH_REACH_SQL, params, cap=1))


class GraphPathsHandler:
    """Handles `GraphPaths`, the ranked routes between two cards from `api.graph_paths`, in one collection.

    `k` caps the routes, and a route is never returned half-read. When more routes exist than `k`,
    the answer says it was cut. The answer is kept within `GRAPH_RESULT_CHARS`.
    """

    def __init__(self, gateway: SqlGateway, collection: str) -> None:
        """Create the handler.

        Args:
            gateway: The read-only SQL gateway.
            collection: The collection every traversal is scoped to.
        """
        self._gateway = gateway
        self._collection = collection

    async def __call__(self, query: GraphPaths, *, tools: frozenset[str] = TOOL_NAMES) -> GraphPathsResult:
        """Run `api.graph_paths` and group its rows by `path_rank` into bounded, ordered `GraphPath` routes.

        Args:
            query: The route search to run.
            tools: The tool names the caller registered. The next move names only these.

        Returns:
            The bounded routes with their next move.
        """
        params = {
            "collection": self._collection,
            "from_id": query.from_id,
            "to_id": query.to_id,
            "max_hops": query.max_hops,
            "rel_types": _named_relations(query.rel_types),
            "k": query.k,
            "include_superseded": query.include_superseded,
        }
        result = await self._gateway.rows(GRAPH_PATHS_SQL, params, cap=_paths_row_cap(query))
        found = _paths_by_rank(result.rows, self._collection)
        kept = found[: _logical_cap(query.k)]
        more = len(found) > len(kept)
        reach = None if found else await self._reach(query)
        return bound_graph_paths(
            kept,
            knowledge_base=None,
            next_steps=paths_next_steps(
                kept,
                from_id=query.from_id,
                to_id=query.to_id,
                truncated=more,
                tools=tools,
                max_hops=route_hops(query.max_hops),
                reach=reach,
            ),
            omitted_items=None if more else 0,
            reach=reach,
        )

    async def _reach(self, query: GraphPaths) -> GraphReach | None:
        """How far the route's start reaches with no hop limit, and the hops to its end, under the search's own filters."""
        params = {
            "collection": self._collection,
            "seed_ids": [query.from_id],
            "rel_types": _named_relations(query.rel_types),
            "target_id": query.to_id,
            "include_superseded": query.include_superseded,
        }
        return _reach_of(await self._gateway.rows(GRAPH_REACH_SQL, params, cap=1))


class StatusHandler:
    """Handles `Status`, the `kb.*` row counts and the embed metadata, read through the admin gateway.

    You must pass the admin gateway. The read-only role has no grant on the `kb` schema.
    """

    def __init__(self, admin_gateway: SqlGateway) -> None:
        """Create the handler.

        Args:
            admin_gateway: The admin SQL gateway.
        """
        self._admin = admin_gateway

    async def __call__(self, query: Status) -> StatusReport:
        """Count each `kb.*` table and read the single `kb.embed_meta` row.

        Args:
            query: The status request, which carries no parameters.

        Returns:
            The row counts and the embed meta.
        """
        del query  # the message carries no parameters
        counts = await self._admin.rows(_status_counts_sql(), cap=len(_STATUS_TABLES))
        embed = await self._admin.rows("SELECT model, dim, embedded_at FROM kb.embed_meta LIMIT 1", cap=1)
        model, dim, embedded_at = _embed_meta(embed)
        return StatusReport(row_counts=_row_counts(counts), embed_model=model, embed_dim=dim, embedded_at=embedded_at)


class KnowledgeBaseInfo(BaseModel):
    """One indexed collection: its page count, page type mix, and last index time."""

    model_config = ConfigDict(frozen=True)

    name: str
    pages: int
    page_types: tuple[str, ...] = ()
    last_indexed: datetime | None = None


class Collections(BaseModel):
    """Every indexed collection, one `KnowledgeBaseInfo` per distinct `kb.pages.collection` value."""

    model_config = ConfigDict(frozen=True)

    collections: tuple[KnowledgeBaseInfo, ...] = ()


# Every collection's page count, sorted page type list and last index time. The last index time is
# NULL for a collection with no fingerprint row yet.
_COLLECTIONS_CAP = 1000
_COLLECTIONS_SQL = """
SELECT p.collection AS collection, count(*) AS pages, array_agg(DISTINCT p.page_type ORDER BY p.page_type) AS page_types, max(f.indexed_at) AS last_indexed
FROM kb.pages p
LEFT JOIN kb.index_fingerprint f ON f.collection = p.collection
GROUP BY p.collection
"""


class KnowledgeBasesHandler:
    """Lists every indexed collection: page count, page type mix, and last index time.

    You must pass the admin gateway. The read-only role has no grant on these tables. The last index
    time is `None` for a collection with pages but no fingerprint row yet.
    """

    def __init__(self, admin_gateway: SqlGateway) -> None:
        """Create the handler.

        Args:
            admin_gateway: The admin SQL gateway.
        """
        self._admin = admin_gateway

    async def __call__(self) -> Collections:
        """Read every collection's page count, page type mix, and last index time.

        Returns:
            One entry for each indexed collection.
        """
        result = await self._admin.rows(_COLLECTIONS_SQL, {}, cap=_COLLECTIONS_CAP)
        return Collections(collections=tuple(_collection_info(row) for row in result.rows))


class ValidationRow(BaseModel):
    """One standing validation issue on an indexed page: the rule, its severity, and where it points."""

    model_config = ConfigDict(frozen=True)

    page_id: str
    severity: Severity
    rule: str
    message: str
    anchor: str | None = None


class KnowledgeBaseValidation(BaseModel):
    """One collection's persisted validation issues, errors before warnings, in page order."""

    model_config = ConfigDict(frozen=True)

    issues: tuple[ValidationRow, ...] = ()


# One collection's persisted validation rows, errors before warnings, then by page and rule.
_VALIDATION_CAP = 2000
_VALIDATION_SQL = """
SELECT page_id, severity, rule, message, anchor
FROM kb.validation_store
WHERE collection = :collection
ORDER BY severity, page_id, rule
"""


class KnowledgeBaseValidationHandler:
    """Reads one collection's standing validation issues from `kb.validation_store`.

    You must pass the admin gateway. The read-only role has no grant on this table. The rows are the
    issues the last index persisted, not a fresh validation run.
    """

    def __init__(self, admin_gateway: SqlGateway) -> None:
        """Create the handler.

        Args:
            admin_gateway: The admin SQL gateway.
        """
        self._admin = admin_gateway

    async def __call__(self, collection: str) -> KnowledgeBaseValidation:
        """Read `collection`'s persisted validation issues, errors first.

        Args:
            collection: The collection to read.

        Returns:
            The persisted issues.
        """
        result = await self._admin.rows(_VALIDATION_SQL, {"collection": collection}, cap=_VALIDATION_CAP)
        return KnowledgeBaseValidation(issues=tuple(_validation_row(row) for row in result.rows))


# -- rendering helpers -------------------------------------------------------


def halfvec_literal(vector: Sequence[float]) -> str:
    """Format `vector` as a pgvector `halfvec` text literal, `[f1,f2,...]`.

    Args:
        vector: The embedding components.

    Returns:
        The bracketed, comma-separated literal.
    """
    return "[" + ",".join(repr(component) for component in vector) + "]"


def _graph_node(row: Row, collection: str) -> GraphNode:
    """Project one `api.cards` row into a `GraphNode` whose ref is the page's ref in `collection`."""
    return GraphNode(
        id=str(row["id"]),
        ref=qualify_ref(f"page:{row['id']!s}", collection),
        type=str(row["type"]),
        title=str(row["title"]),
        status=str(row["status"]),
        superseded=bool(row["superseded"]),
    )


def _graph_edge(row: Row, collection: str) -> GraphEdge:
    """Project one `api.edges` row into a `GraphEdge`, its claim as a `unit:` ref when it has one."""
    provenance = row["provenance_claim"]
    return GraphEdge(
        src_id=str(row["src_id"]),
        dst_id=str(row["dst_id"]),
        rel=str(row["rel"]),
        provenance_claim=None if provenance is None else str(provenance),
        provenance_ref=None if provenance is None else qualify_ref(f"unit:{provenance!s}", collection),
    )


def _unique_nodes(rows: Sequence[Row], collection: str) -> tuple[GraphNode, ...]:
    """Project `rows` to `GraphNode`, keeping the first occurrence of each id (one node per row)."""
    seen: dict[str, GraphNode] = {}
    for row in rows:
        node = _graph_node(row, collection)
        seen.setdefault(node.id, node)
    return tuple(seen.values())


def _reaching_edges(rows: Sequence[Row], collection: str) -> tuple[GraphEdge, ...]:
    """Project the edge that reached each node, skipping rows whose `rel` is NULL (a seed has no in-edge)."""
    return tuple(_graph_edge(row, collection) for row in rows if row.get("rel") is not None)


def _edges_between(edges: Sequence[GraphEdge], nodes: Sequence[GraphNode]) -> tuple[GraphEdge, ...]:
    """The edges of `edges` whose two endpoints are both in `nodes`, in their original order."""
    reached = {node.id for node in nodes}
    return tuple(edge for edge in edges if edge.src_id in reached and edge.dst_id in reached)


def _named_relations(rel_types: Sequence[str] | None) -> list[str] | None:
    """The relations a traversal may travel, or `None` (every relation) when the caller named none."""
    return list(rel_types) if rel_types else None


def _logical_cap(k: int) -> int:
    """The number of logical items a traversal answers with: the caller's `k` under the traversal's own clamp."""
    return min(max(k, 1), GRAPH_TOP_K_CAP)


def _paths_row_cap(query: GraphPaths) -> int:
    """The rows a capped route set can occupy, one route past `k` at `max_hops + 1` nodes each, under the row ceiling."""
    return min(_GRAPH_ROW_CAP, (_logical_cap(query.k) + 1) * (max(query.max_hops, 1) + 1))


def expansion_hops(hops: int) -> int:
    """The hops an expansion walks for a requested `hops`, held between 1 and `GRAPH_EXPAND_HOP_LIMIT`.

    Args:
        hops: The hops the caller asked for.

    Returns:
        The hops the walk takes.
    """
    return min(max(hops, 1), GRAPH_EXPAND_HOP_LIMIT)


def route_hops(max_hops: int) -> int:
    """The longest route a search looks for under a requested `max_hops`, held between 1 and `GRAPH_PATHS_HOP_LIMIT`.

    Args:
        max_hops: The route length the caller asked for.

    Returns:
        The longest route the search looks for.
    """
    return min(max(max_hops, 1), GRAPH_PATHS_HOP_LIMIT)


def _reach_of(result: SqlResult) -> GraphReach | None:
    """The one row `api.graph_reach` returned, or `None` when the collection gave none."""
    return GraphReach.model_validate(dict(result.rows[0])) if result.rows else None


def graph_next_steps(  # noqa: PLR0913 (the subgraph, its cut, its tools, its seeds, its hops and its reach are each a distinct input)
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    *,
    truncated: bool,
    tools: frozenset[str],
    seed_ids: Sequence[str],
    hops: int = 2,
    reach: GraphReach | None = None,
) -> NextSteps:
    """The next-move block an expansion carries: read a reached node, verify a relation, or narrow or widen the walk.

    An option is offered only when `tools` holds the tool it names. An empty neighbourhood offers the
    two reasons it may be empty: the seed may not be a node id, or the branch may be superseded. With a
    `reach`, the summary says whether nodes lie past the last hop, and how far, or that the answer holds
    every node the seeds reach.

    Args:
        nodes: The reached nodes the answer carries, in traversal order.
        edges: The relations between them.
        truncated: Whether nodes were left out of the answer.
        tools: The canonical tool names the active profile exposes.
        seed_ids: The seeds the walk started from, quoted back when nothing was reached.
        hops: The hops the walk took, after its limit.
        reach: How far the seeds reach with no hop limit, or `None` when that was not read.

    Returns:
        The next-move block.
    """
    if not nodes:
        seed = seed_ids[0] if seed_ids else ""
        options = ["Re-run with include_superseded=true: a replaced page and everything behind it is skipped by default."]
        if SEARCH in tools:
            options.append(f"search(query=...) to ground the seed id: {seed!r} may not be a node in this knowledge base.")
        return NextSteps(summary=f"No current node was reached from {seed!r}.", options=tuple(options))
    asserted = next((edge for edge in edges if edge.provenance_ref is not None), None)
    options: list[str] = []
    if FETCH in tools:
        options.append(f"fetch(unit_id={nodes[0].ref!r}) to read a reached page before relying on what it connects to.")
        if asserted is not None:
            options.append(f"fetch(unit_id={asserted.provenance_ref!r}) to read the claim asserting one of these relations.")
    if truncated:
        options.append("Expand again with hops=1, a rel_types filter, or a smaller k to see a narrower neighbourhood in full.")
    wider = _wider_hops(hops, reach)
    if wider is not None:
        options.append(f"graph_expand(seed_ids={list(seed_ids)!r}, hops={wider}) to reach further out.")
    return NextSteps(
        summary=f"{len(nodes)} node(s) and {len(edges)} relation(s) reached. {_ASSERTION_CLAUSE}{_reach_sentence(hops, reach)}",
        options=tuple(options),
    )


def _wider_hops(hops: int, reach: GraphReach | None) -> int | None:
    """The `hops` a wider expansion should ask for, or `None` when nothing lies past the walk or it is at its limit."""
    if reach is None or hops >= GRAPH_EXPAND_HOP_LIMIT:
        return None
    if reach.deepest_hop > hops:
        return min(reach.deepest_hop, GRAPH_EXPAND_HOP_LIMIT)
    return None if reach.complete else GRAPH_EXPAND_HOP_LIMIT


def _reach_sentence(hops: int, reach: GraphReach | None) -> str:
    """What the expansion's summary says about the nodes past its last hop, or nothing when its reach was not read."""
    if reach is None:
        return ""
    if not reach.complete:
        return f" The seeds reach more than {GRAPH_REACH_NODE_LIMIT:,} nodes, so nodes may lie past hop {hops}.{_hop_limit(hops)}"
    if reach.deepest_hop > hops:
        return (
            f" Nodes lie past hop {hops}: the seeds reach {reach.reached_nodes} node(s), the farthest {reach.deepest_hop} hops out."
            f"{_hop_limit(hops)}"
        )
    return " These are all the nodes the seeds reach."


def _hop_limit(hops: int) -> str:
    """The sentence an expansion at its hop limit adds, or nothing below it."""
    return f" An expansion walks at most {GRAPH_EXPAND_HOP_LIMIT} hops." if hops >= GRAPH_EXPAND_HOP_LIMIT else ""


def paths_next_steps(  # noqa: PLR0913 (the routes, both endpoints, their cut, the tools, the depth searched and the reach are each a distinct input)
    paths: Sequence[GraphPath],
    *,
    from_id: str,
    to_id: str,
    truncated: bool,
    tools: frozenset[str],
    max_hops: int = 4,
    reach: GraphReach | None = None,
) -> NextSteps:
    """The next-move block a route list carries: verify a relation, or take the ways out of an empty answer.

    An empty answer says how far the search looked. With a `reach`, it says that no route exists at
    any length, or how long the shortest one is, or how many nodes the start reaches without arriving
    at the end. It offers a longer search while one could find a route, the reverse direction and a
    shared-neighbour expansion. An option is offered only when `tools` holds the tool it names.

    Args:
        paths: The routes the answer carries, in rank order.
        from_id: The route's start, quoted back when nothing was found.
        to_id: The route's end, quoted back when nothing was found.
        truncated: Whether routes were left out of the answer.
        tools: The canonical tool names the active profile exposes.
        max_hops: The most hops the search looked, after its cap.
        reach: How far the start reaches with no hop limit and the hops to the end, or `None` when that was not read.

    Returns:
        The next-move block.
    """
    if not paths:
        longer = _longer_search(max_hops, reach)
        options = [] if longer is None else [f"find_paths(from_id={from_id!r}, to_id={to_id!r}, max_hops={longer}) to look for a longer route."]
        options.append(f"find_paths(from_id={to_id!r}, to_id={from_id!r}): routes follow edge direction, so try the reverse.")
        if GRAPH_EXPAND in tools:
            options.append(f"graph_expand(seed_ids=[{from_id!r}, {to_id!r}]) to surface a shared neighbour instead of a route.")
        options.append("Re-run with include_superseded=true: a route through a replaced page is skipped by default.")
        return NextSteps(summary=_no_route_summary(from_id, to_id, max_hops, reach), options=tuple(options))
    asserted = next((edge for path in paths for edge in path.edges if edge.provenance_ref is not None), None)
    options = []
    if FETCH in tools and asserted is not None:
        options.append(f"fetch(unit_id={asserted.provenance_ref!r}) to read the claim asserting one of these relations.")
    if truncated:
        options.append("Search again with a smaller max_hops, a rel_types filter, or a smaller k to see fewer routes in full.")
    return NextSteps(
        summary=f"{len(paths)} route(s) from {from_id!r} to {to_id!r}, shortest first. {_ASSERTION_CLAUSE}",
        options=tuple(options),
    )


def _longer_search(max_hops: int, reach: GraphReach | None) -> int | None:
    """The `max_hops` a longer route search should ask for, or `None` when none could find a route."""
    if reach is None:
        return GRAPH_PATHS_HOP_LIMIT if max_hops < GRAPH_PATHS_HOP_LIMIT else None
    if reach.target_hop is not None:
        return reach.target_hop if max_hops < reach.target_hop <= GRAPH_PATHS_HOP_LIMIT else None
    return None if reach.complete or max_hops >= GRAPH_PATHS_HOP_LIMIT else GRAPH_PATHS_HOP_LIMIT


def _no_route_summary(from_id: str, to_id: str, max_hops: int, reach: GraphReach | None) -> str:
    """What an empty route list says: how far the search looked, and what the start's reach shows beyond it."""
    within = f"No route from {from_id!r} to {to_id!r} within {max_hops} hops."
    if reach is None:
        if max_hops >= GRAPH_PATHS_HOP_LIMIT:
            return f"{within} Routes are searched up to {GRAPH_PATHS_HOP_LIMIT} hops, so a longer one is not ruled out."
        return within
    if reach.target_hop is not None:
        if reach.target_hop > GRAPH_PATHS_HOP_LIMIT:
            return f"{within} The shortest route takes {reach.target_hop} hops, past the {GRAPH_PATHS_HOP_LIMIT} hops a route search looks."
        return f"{within} The shortest route takes {reach.target_hop} hops."
    if reach.complete:
        return f"No route from {from_id!r} to {to_id!r} exists: {to_id!r} is not among the {reach.reached_nodes} node(s) {from_id!r} reaches."
    return f"{within} {from_id!r} reaches more than {GRAPH_REACH_NODE_LIMIT:,} nodes without {to_id!r} among them, so a longer route is not ruled out."


def _graph_window(*, returned: int, dropped: int, omitted_items: int | None, reason: str) -> ResultWindow:
    """How complete a bounded graph answer is, adding the budget's drops to a known omitted count only."""
    total = None if omitted_items is None else omitted_items + dropped
    short = total is None or total > 0
    return ResultWindow(returned_items=returned, omitted_items=total, truncated=short, reason=reason if short else None)


def _shortened_node(node: GraphNode, limit: int) -> GraphNode:
    """`node` with only its display title bounded to `limit`: the id, the ref and the lifecycle are untouched."""
    return node.model_copy(update={"title": clip_text(node.title, limit) or ""})


def _shortened_path(path: GraphPath, limit: int) -> GraphPath:
    """`path` with every node's display title bounded to `limit`: ids, refs and provenance are untouched."""
    return path.model_copy(update={"nodes": tuple(_shortened_node(node, limit) for node in path.nodes)})


def bound_graph_result(  # noqa: PLR0913 (the subgraph, its stamp, its next move, what was already omitted, the budget and the reach are each a distinct input)
    nodes: tuple[GraphNode, ...],
    edges: tuple[GraphEdge, ...],
    *,
    knowledge_base: KnowledgeBaseStamp | None,
    next_steps: NextSteps,
    omitted_items: int | None = 0,
    max_chars: int = GRAPH_RESULT_CHARS,
    reach: GraphReach | None = None,
) -> GraphResult:
    """The largest prefix of `nodes`, with the edges between them, whose complete serialized answer fits `max_chars`.

    Traversal order is preserved and a node's id and ref are never shortened. A node is added whole
    when it fits, else with its title shortened, else not at all, and every later node and every edge
    touching a dropped node is left out with it. If not even the first node fits, the answer is an
    empty refusal.

    Args:
        nodes: The reached nodes, in traversal order.
        edges: The relations between them. An edge is kept while both its endpoints are.
        knowledge_base: The stamp of the knowledge base that answered, or `None` when unstamped.
        next_steps: The next move this neighbourhood offers.
        omitted_items: What the caller already knew was left out (`None` once a probe proved more
            exist without counting them).
        max_chars: The longest complete serialized answer to return.
        reach: How far the seeds reach with no hop limit, carried on the answer, or `None`.

    Returns:
        The bounded subgraph, its window saying how much of the traversal it carries.
    """

    def answer(kept: tuple[GraphNode, ...], *, dropped: int) -> GraphResult:
        return GraphResult(
            nodes=kept,
            edges=_edges_between(edges, kept),
            window=_graph_window(returned=len(kept), dropped=dropped, omitted_items=omitted_items, reason=_GRAPH_TRUNCATION_REASON),
            reach=reach,
            knowledge_base=knowledge_base,
            next_steps=next_steps,
        )

    kept: tuple[GraphNode, ...] = ()
    for index, candidate in enumerate(nodes):
        fitted = longest_fitting(
            candidate,
            shorten=_shortened_node,
            fits=lambda node, kept=kept, index=index: fits_budget(answer((*kept, node), dropped=len(nodes) - index - 1), max_chars),
            longest=len(candidate.title),
        )
        if fitted is None:
            break
        kept = (*kept, fitted)
    if nodes and not kept:
        refused = GraphResult(
            window=ResultWindow(
                returned_items=0,
                omitted_items=None if omitted_items is None else omitted_items + len(nodes),
                truncated=True,
                refused=True,
                reason=_GRAPH_REFUSAL_REASON,
            ),
            knowledge_base=knowledge_base,
            next_steps=_GRAPH_REFUSAL_STEPS,
        )
        return with_fallback_steps(refused, max_chars, steps=_MINIMAL_GRAPH_STEPS)
    return with_fallback_steps(answer(kept, dropped=len(nodes) - len(kept)), max_chars, steps=_MINIMAL_GRAPH_STEPS)


def bound_graph_paths(  # noqa: PLR0913 (the routes, their stamp, their next move, what was already omitted, the budget and the reach are each a distinct input)
    paths: tuple[GraphPath, ...],
    *,
    knowledge_base: KnowledgeBaseStamp | None,
    next_steps: NextSteps,
    omitted_items: int | None = 0,
    max_chars: int = GRAPH_RESULT_CHARS,
    reach: GraphReach | None = None,
) -> GraphPathsResult:
    """The largest prefix of `paths` whose complete serialized answer fits `max_chars`.

    Rank order is preserved and a route is kept whole or not at all. A route is added untouched when
    it fits, else with its nodes' titles shortened, else not at all, and every later route is left out
    with it. If not even the first route fits, the answer is an empty refusal.

    Args:
        paths: The ranked routes, shortest first.
        knowledge_base: The stamp of the knowledge base that answered, or `None` when unstamped.
        next_steps: The next move these routes offer.
        omitted_items: What the caller already knew was left out (`None` once a probe proved more
            exist without counting them).
        max_chars: The longest complete serialized answer to return.
        reach: How far the start reaches with no hop limit, carried on the answer, or `None`.

    Returns:
        The bounded routes, their window saying how much of the ranked list they carry.
    """

    def answer(kept: tuple[GraphPath, ...], *, dropped: int) -> GraphPathsResult:
        return GraphPathsResult(
            paths=kept,
            next_steps=next_steps,
            knowledge_base=knowledge_base,
            window=_graph_window(returned=len(kept), dropped=dropped, omitted_items=omitted_items, reason=_PATHS_TRUNCATION_REASON),
            reach=reach,
        )

    kept: tuple[GraphPath, ...] = ()
    for index, candidate in enumerate(paths):
        fitted = longest_fitting(
            candidate,
            shorten=_shortened_path,
            fits=lambda path, kept=kept, index=index: fits_budget(answer((*kept, path), dropped=len(paths) - index - 1), max_chars),
            longest=max((len(node.title) for node in candidate.nodes), default=0),
        )
        if fitted is None:
            break
        kept = (*kept, fitted)
    if paths and not kept:
        refused = GraphPathsResult(
            next_steps=_PATHS_REFUSAL_STEPS,
            knowledge_base=knowledge_base,
            window=ResultWindow(
                returned_items=0,
                omitted_items=None if omitted_items is None else omitted_items + len(paths),
                truncated=True,
                refused=True,
                reason=_PATHS_REFUSAL_REASON,
            ),
        )
        return with_fallback_steps(refused, max_chars, steps=_MINIMAL_GRAPH_STEPS)
    return with_fallback_steps(answer(kept, dropped=len(paths) - len(kept)), max_chars, steps=_MINIMAL_GRAPH_STEPS)


def routed_graph_result(
    result: GraphResult,
    *,
    knowledge_base: KnowledgeBaseStamp | None,
    next_steps: NextSteps,
    max_chars: int = GRAPH_RESULT_CHARS,
) -> GraphResult:
    """The handler's subgraph as the selected route serves it: its own stamp, and a next move the caller can act on.

    The answer is bounded again only when the stamp pushes it past `max_chars`. A refusal keeps its
    own next move and only takes the stamp.

    Args:
        result: The handler's bounded subgraph.
        knowledge_base: The stamp of the knowledge base that answered, or `None` when unstamped.
        next_steps: The next move this surface offers, naming only tools it exposes.
        max_chars: The longest complete serialized answer to return.

    Returns:
        The answer that goes out.
    """
    if result.window.refused:
        return with_fallback_steps(result.model_copy(update={"knowledge_base": knowledge_base}), max_chars, steps=_GRAPH_REFUSAL_STEPS)
    routed = result.model_copy(update={"knowledge_base": knowledge_base, "next_steps": next_steps})
    if fits_budget(routed, max_chars):
        return routed
    return bound_graph_result(
        result.nodes,
        result.edges,
        knowledge_base=knowledge_base,
        next_steps=next_steps,
        omitted_items=result.window.omitted_items,
        max_chars=max_chars,
    )


def routed_graph_paths(
    result: GraphPathsResult,
    *,
    knowledge_base: KnowledgeBaseStamp | None,
    next_steps: NextSteps,
    max_chars: int = GRAPH_RESULT_CHARS,
) -> GraphPathsResult:
    """The handler's routes as the selected route serves them: its own stamp, and a next move the caller can act on.

    The answer is bounded again only when the stamp pushes it past `max_chars`. A refusal keeps its
    own next move and only takes the stamp.

    Args:
        result: The handler's bounded routes.
        knowledge_base: The stamp of the knowledge base that answered, or `None` when unstamped.
        next_steps: The next move this surface offers, naming only tools it exposes.
        max_chars: The longest complete serialized answer to return.

    Returns:
        The answer that goes out.
    """
    if result.window.refused:
        return with_fallback_steps(result.model_copy(update={"knowledge_base": knowledge_base}), max_chars, steps=_PATHS_REFUSAL_STEPS)
    routed = result.model_copy(update={"knowledge_base": knowledge_base, "next_steps": next_steps})
    if fits_budget(routed, max_chars):
        return routed
    return bound_graph_paths(
        result.paths,
        knowledge_base=knowledge_base,
        next_steps=next_steps,
        omitted_items=result.window.omitted_items,
        max_chars=max_chars,
    )


def _paths_by_rank(rows: Sequence[Row], collection: str) -> tuple[GraphPath, ...]:
    """Group `rows` by `path_rank` (preserving first-seen order) into one `GraphPath` each."""
    grouped: dict[object, list[Row]] = {}
    for row in rows:
        grouped.setdefault(row["path_rank"], []).append(row)
    return tuple(_graph_path(group, collection) for group in grouped.values())


def _graph_path(rows: Sequence[Row], collection: str) -> GraphPath:
    """Assemble one path from its (ordered) rows: the nodes, the edges between them, and its score."""
    return GraphPath(
        nodes=tuple(_graph_node(row, collection) for row in rows),
        edges=_reaching_edges(rows, collection),
        score=float(str(rows[0]["score"])),
    )


def _status_counts_sql() -> str:
    """A single UNION ALL of `SELECT '<t>', count(*) FROM memory.<t>` over the table allowlist."""
    legs = [f"SELECT '{table}' AS table, count(*) AS n FROM memory.{table}" for table in _STATUS_TABLES]  # noqa: S608 (fixed allowlist)
    return " UNION ALL ".join(legs)


def _row_counts(result: SqlResult) -> tuple[tuple[str, int], ...]:
    """Project the counts result into `(table, n)` pairs, tolerating int/str cell values."""
    return tuple((str(row["table"]), int(str(row["n"]))) for row in result.rows)


def _embed_meta(result: SqlResult) -> tuple[str | None, int | None, str | None]:
    """The embed-meta row as `(model, dim, embedded_at)` (all `None` before the first index)."""
    if not result.rows:
        return None, None, None
    row = result.rows[0]
    model, dim, embedded_at = row.get("model"), row.get("dim"), row.get("embedded_at")
    return (
        None if model is None else str(model),
        None if dim is None else int(str(dim)),
        None if embedded_at is None else str(embedded_at),
    )


def _collection_info(row: Row) -> KnowledgeBaseInfo:
    """Project one grouped `kb.pages` row into a `KnowledgeBaseInfo`."""
    return KnowledgeBaseInfo(
        name=str(row["collection"]),
        pages=int(str(row["pages"])),
        page_types=_page_types(row["page_types"]),
        last_indexed=_last_indexed(row["last_indexed"]),
    )


def _validation_row(row: Row) -> ValidationRow:
    """Project one `kb.validation_store` row into a `ValidationRow` (anchor is nullable, severity validated against the closed set)."""
    anchor = row["anchor"]
    return ValidationRow.model_validate(
        {
            "page_id": str(row["page_id"]),
            "severity": str(row["severity"]),
            "rule": str(row["rule"]),
            "message": str(row["message"]),
            "anchor": None if anchor is None else str(anchor),
        }
    )


def _page_types(cell: object) -> tuple[str, ...]:
    """Narrow an `array_agg` cell (a list from the array codec) to a `page_types` tuple."""
    return tuple(str(item) for item in cell) if isinstance(cell, list) else ()


def _last_indexed(cell: object) -> datetime | None:
    """Narrow a `index_fingerprint.indexed_at` cell, `None` for the left join's miss case."""
    return cell if isinstance(cell, datetime) else None


def _render_schema(description: SchemaDescription) -> str:
    """Render a `SchemaDescription` as M-Schema text, shortening every comment."""
    rendered = [_render_object(obj) for obj in description.objects]
    body = "\n\n".join(text for text, _clipped in rendered)
    if any(clipped for _text, clipped in rendered):
        return f"{body}\n\n{_SCHEMA_FIELD_CLIPPED_NOTE}"
    return body


def _render_object(obj: ObjectInfo) -> tuple[str, bool]:
    """One object block (`# <name> (<kind>), <comment>` then one line per column) and whether anything was cut."""
    header = f"# {obj.name} ({obj.kind})"
    comment, comment_clipped = _clipped_comment(obj.comment)
    if comment:
        header += f": {comment}"
    columns = [_render_column(column) for column in obj.columns]
    lines = "\n".join([header, *(text for text, _clipped in columns)])
    return lines, comment_clipped or any(clipped for _text, clipped in columns)


def _render_column(column: ColumnInfo) -> tuple[str, bool]:
    """One column line (`- <name> <type>: <comment>`) and whether its comment was cut."""
    line = f"- {column.name} {column.data_type}"
    comment, clipped = _clipped_comment(column.comment)
    return (f"{line}: {comment}" if comment else line), clipped


def _clipped_comment(comment: str | None) -> tuple[str | None, bool]:
    """`comment` bounded to `_SCHEMA_FIELD_CHARS`, and whether bounding changed it."""
    clipped = clip_text(comment, _SCHEMA_FIELD_CHARS)
    return clipped, clipped != comment


def _one_line_purpose(comment: str | None) -> tuple[str, bool]:
    """`comment`'s first sentence, bounded to `_SCHEMA_PURPOSE_CHARS`, and whether it was cut."""
    if not comment:
        return "(no description)", False
    head = comment.split(". ", 1)[0]
    purpose = head if head == comment else f"{head}."
    clipped = clip_text(purpose, _SCHEMA_PURPOSE_CHARS) or ""
    return clipped, clipped != purpose


def _render_catalog(description: SchemaDescription) -> str:
    """Every object's name, kind and one-line purpose, in the gateway's own order, plus the ask-for-detail line."""
    lines: list[str] = []
    any_clipped = False
    for obj in description.objects:
        purpose, clipped = _one_line_purpose(obj.comment)
        any_clipped = any_clipped or clipped
        lines.append(f"- {obj.name} ({obj.kind}): {purpose}")
    lines.append("")
    if any_clipped:
        lines.extend([_SCHEMA_FIELD_CLIPPED_NOTE, ""])
    lines.append(DESCRIBE_SCHEMA_CATALOG_INSTRUCTION)
    return "\n".join(lines)


def _grounding_block(spec: KnowledgeBaseSchemaView) -> str:
    """One knowledge base's slug, page types, example ids and decision page types, for a schema answer that covers several."""
    page_types = tuple(spec.page_types)
    primary, secondary = _example_ids(page_types)
    decisions = tuple(page_type.page_type for page_type in page_types if page_type.decision_declared)
    return "\n".join(
        (
            f"knowledge_base {spec.slug}",
            f"- page types: {'|'.join(page_type.page_type for page_type in page_types) if page_types else 'none declared'}",
            f"- example ids: {primary if secondary is None else f'{primary}, {secondary}'}",
            f"- decision page types: {'|'.join(decisions) if decisions else 'none, this knowledge base declares no decision page type'}",
        )
    )


def _example_ids(page_types: Sequence[PageTypeView]) -> tuple[str, str | None]:
    """This knowledge base's own first and second page types, shaped as example ids."""
    if not page_types:
        return "example", None
    primary = _example_id(page_types[0])
    secondary = _example_id(page_types[1]) if len(page_types) > 1 else None
    return primary, secondary


def _example_id(page_type: PageTypeView) -> str:
    """One page id shaped like `page_type`'s own ids: its `id_prefix` (or the page_type itself) plus `example`."""
    return f"{page_type.id_prefix}example" if page_type.id_prefix else f"{page_type.page_type}.example"
