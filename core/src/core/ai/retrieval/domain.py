"""The value types of retrieval: the queries a caller builds and the result models a handler fills.

A consumer extends the queries with its own facets and fills the result models from its own SQL surface.
The queries are reads. Each is a frozen model and never a bus message.
The field descriptions of every result model are the output schema an agent reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# One result row as a gateway yields it: column name to cell value. A cell may be a datetime, Decimal,
# UUID and so on, and a handler narrows each column it reads.
type Row = Mapping[str, object]


# -- agent feedback (the submit-feedback meta-tool) --------------------------

# What a piece of agent feedback is about: an unclear or misleading tool `usage` instruction, a
# `feature` request, a `nice-to-have`, a `bottleneck` or slow path, a `bug`, or `other`.
FeedbackCategory = Literal["usage", "feature", "nice-to-have", "bottleneck", "bug", "other"]


class _Query(BaseModel):
    """The frozen base of every retrieval query."""

    model_config = ConfigDict(frozen=True)


class FeedbackEntry(BaseModel):
    """One piece of feedback a consuming agent submits about the retrieval tools.

    A `FeedbackSink` records it append-only so an operator can review it later.
    """

    model_config = ConfigDict(frozen=True)

    created_at: str  # ISO-8601 UTC, stamped by the sink at record time
    category: FeedbackCategory
    message: str
    tool: str | None = None  # the tool the feedback is about, when the agent names one


# -- query messages (frozen reads) -------------------------------------------


class Search(_Query):
    """Hybrid search over the knowledge base, the generic core a consumer extends with its own facets.

    `hybrid=False` skips the query embedding and runs the keyword-only fusion leg (a NULL query
    vector). `k` is the requested row count (a server caps it, and a caller may ride it as the row
    cap). A consumer subclasses this to add the facet fields that narrow its own knowledge base before
    ranking.
    """

    query: str
    k: int = 12
    hybrid: bool = True


class Fetch(_Query):
    """Fetch one unit's full text by its id."""

    id: str


class LookupValue(_Query):
    """Ground a facet's allowed values so a caller never guesses filter strings."""

    facet: str
    term: str | None = None


class DescribeSchema(_Query):
    """Render the schema-description text (comments plus types) of the retrieval surface.

    With no `objects` named, the answer is a compact catalog of every object's name, kind and
    one-line purpose. Naming one or more `objects` answers full column detail for just those.
    """

    objects: tuple[str, ...] = ()


class Status(_Query):
    """Report indexed knowledge base health: row counts plus the embed-model contract and last index."""


class Graph(_Query):
    """The whole knowledge base graph for client-side rendering.

    Both filters are client-narrowing aids applied before the graph crosses the wire: `page_type`
    filters the node type, edges are then restricted to surviving endpoints, and `rel` filters the
    edge relation, leaving the node set whole. With neither set the result is the entire graph.
    """

    page_type: str | None = None
    rel: str | None = None


class GraphExpand(_Query):
    """Expand the neighbourhood around `seed_ids` out to `hops` typed relations.

    A multi-hop walk over the edge graph: starting from `seed_ids`, follow edges up to `hops` deep,
    optionally restricted to `rel_types` (`None` traverses every relation). `k` caps the returned
    neighbour nodes, and the result is the seed-plus-reached subgraph. Superseded nodes are skipped
    at every hop, so nothing reachable only through one comes back either. `include_superseded`
    walks them anyway, for reading what a replaced page used to connect to.
    """

    seed_ids: tuple[str, ...]
    hops: int = 2
    rel_types: tuple[str, ...] | None = None
    k: int = 20
    include_superseded: bool = False


class GraphPaths(_Query):
    """Find the typed paths from `from_id` to `to_id` within `max_hops` edges.

    A bounded path search over the edge graph between two nodes, optionally restricted to `rel_types`
    (`None` traverses every relation). `k` caps the number of returned paths, each a `GraphPath` of
    the nodes and edges along the route. A route through a superseded node is skipped.
    `include_superseded` keeps it, for tracing how two nodes used to connect.
    """

    from_id: str
    to_id: str
    max_hops: int = 4
    rel_types: tuple[str, ...] | None = None
    k: int = 10
    include_superseded: bool = False


# -- result models -----------------------------------------------------------

# Which retrieval legs actually selected a search hit: both the lexical (BM25) and semantic (vector)
# legs, the lexical leg alone, or the semantic leg alone. This is leg PRESENCE, not a correctness or
# relevance judgment: a lexical match means the query's terms matched, a semantic match means the
# embedding placed it nearby, neither proves the hit answers the question.
SearchSupport = Literal["lexical_and_semantic", "lexical_only", "semantic_only"]

# What a `search` call actually found, so a caller reads the right confidence into a result instead
# of assuming every returned hit is equally trustworthy: `unassessed` (support was not computed),
# `no_hits`, `lexically_supported_candidates` (at least one hit had lexical support: still
# unverified evidence, not a confirmed answer), `semantic_candidates_only` (every hit was
# vector-only: weaker evidence, verify by fetching), or `budget_refused` (the result could not fit
# its size budget at all).
SearchResultStatus = Literal["unassessed", "no_hits", "lexically_supported_candidates", "semantic_candidates_only", "budget_refused"]


class ResultWindow(BaseModel):
    """How much of an underlying result a bounded response actually carries.

    Every bounded result (a search, a graph expansion, a set of paths) carries one, so a caller reads a
    short result as "capped, more exists" rather than "the whole answer". A `refused` window means the result
    carries zero items because even the first one's mandatory identity would not fit. `reason` then names the
    narrower call to make instead, never a partial or corrupted item.
    """

    model_config = ConfigDict(frozen=True)

    returned_items: int = Field(default=0, ge=0, description="How many items this result actually carries.")
    omitted_items: int | None = Field(
        default=0,
        ge=0,
        description="How many further items exist beyond what was returned. 0 means the result is complete. A positive "
        "number is an exact count of what was left out. None means at least one more item is known to exist (a "
        "one-past-the-cap probe found it) but the exact remainder was never counted. Read None as 'more, exact count "
        "unknown', never as zero.",
    )
    truncated: bool = Field(
        default=False, description="True when this result was cut short of the underlying total, whatever omitted_items says."
    )
    refused: bool = Field(
        default=False,
        description="True when no result could be built within the size budget at all: returned_items is 0 and reason "
        "names a narrower next call. False (the default) means at least an empty-but-valid result was returned.",
    )
    reason: str | None = Field(
        default=None,
        description="Why the result was truncated or refused, and what narrower call to try next. Always set (and "
        "nonempty) when refused is true. It is optional otherwise.",
    )

    @model_validator(mode="after")
    def _refusal_carries_no_items_and_names_a_next_call(self) -> ResultWindow:
        """A refused window is a valid empty result, never a partial one and never a silent one."""
        if self.refused and (self.returned_items != 0 or not (self.reason and self.reason.strip())):
            msg = "a refused ResultWindow must carry returned_items=0 and a nonempty reason naming the next call"
            raise ValueError(msg)
        return self


class SqlResult(BaseModel):
    """The row-capped result of any read through the gateway."""

    model_config = ConfigDict(frozen=True)

    rows: tuple[Row, ...] = Field(default=(), description="The result rows, already capped. Each maps a column name to its value.")
    columns: tuple[str, ...] = Field(default=(), description="The result's column order, for reading rows as a table.")
    truncated_message: str | None = Field(
        default=None,
        description="Set when the cap trimmed the result ('… N more rows'): narrow the query instead of re-running it.",
    )


class FoldedUnit(BaseModel):
    """One more matching unit of a hit's document, listed under that hit rather than ranked as a hit of its own."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="The unit's id.")
    hit_level: str = Field(description="The unit's granularity tier, one of the values a hit's hit_level takes.")
    ref: str | None = Field(
        default=None,
        description="The exact collection-qualified handle to pass to fetch, set by the search tool. None on a row read "
        "straight from the search function.",
    )


class SearchHit(BaseModel):
    """One ranked hit from a search (the read counterpart of the search function's result row)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="The matched unit's id. Fetch it to read the source.")
    ref: str | None = Field(
        default=None,
        description="The exact collection-qualified handle to pass to fetch, set by the read tool. None on a row read straight "
        "from the database function.",
    )
    hit_level: str = Field(
        description="This hit's granularity tier: how big the matched unit is. The legal values are "
        "knowledge-base-specific. The search tool's own description names them."
    )
    page_id: str | None = Field(default=None, description="The page this hit sits inside, when the hit is smaller than a page.")
    page_title: str | None = Field(default=None, description="That page's display title.")
    snippet: str | None = Field(default=None, description="The matched text, trimmed, enough to judge relevance, never the source.")
    snippet_truncated: bool = Field(
        default=False, description="True when snippet was cut short of the matched text. False means snippet, if present, is complete."
    )
    score: float = Field(description="Fused relevance, higher ranks first. Compare only within one knowledge base's hits.")
    rerank_score: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="The re-ranking model's probability that this hit helps answer the query. It means the same across "
        "queries and knowledge bases. None when no model re-ranked this search.",
    )
    answered_by: Literal["lexical", "hybrid"] | None = Field(
        default=None, description="Which retrieval leg ranked this response: lexical or hybrid."
    )
    lexical_rank: int | None = Field(
        default=None, ge=1, description="This hit's rank on the lexical (BM25 keyword) leg alone, or None when that leg did not select it."
    )
    vector_rank: int | None = Field(
        default=None, ge=1, description="This hit's rank on the semantic (vector) leg alone, or None when that leg did not select it."
    )
    vector_distance: float | None = Field(
        default=None,
        description="This hit's raw vector distance on the semantic leg, lower is closer. None when the semantic leg did not "
        "select it. Comparable only among hits in this result list: nothing gates on it, and the same distance does not "
        "mean the same relevance in another knowledge base or index.",
    )
    support: SearchSupport | None = Field(
        default=None,
        description="Which legs actually selected this hit: lexical_and_semantic, lexical_only, or semantic_only. This is "
        "leg presence, not a correctness or relevance judgment. A supported hit is still unverified evidence until fetched "
        "and read. None on a row from before component support was tracked.",
    )
    knowledge_base: str | None = Field(
        default=None, description="The knowledge base this hit came from: the name a `search` takes as its `knowledge_base` argument."
    )
    folded_units: tuple[FoldedUnit, ...] = Field(
        default=(),
        description="Other units of the document this hit stands for that also matched, best first, listed here instead of as "
        "hits of their own. Fetch one by its ref to read that part without searching again.",
    )


class NextSteps(BaseModel):
    """The next-move block a `search` or `fetch` result carries: what you're holding, what to try next.

    Every retrieval result carries one, so a caller reads it for its next tool call rather than
    guessing one from context alone.
    """

    model_config = ConfigDict(frozen=True)

    summary: str = Field(description="One line naming what this result is.")
    options: tuple[str, ...] = Field(
        default=(), description="Ready-to-use next calls, each a self-contained sentence naming the exact argument to pass."
    )


class KnowledgeBaseStamp(BaseModel):
    """Which knowledge base answered, and how current its indexed copy is, riding every search and fetch answer."""

    model_config = ConfigDict(frozen=True)

    slug: str = Field(description="The knowledge base that answered, by the slug its knowledge-base-schema.json declares.")
    indexed_at: datetime | None = Field(
        default=None,
        description="When this knowledge base was last indexed successfully. None means it has never been indexed, so nothing served is current.",
    )
    changed_since: int | None = Field(
        default=None,
        ge=0,
        description="How many files differ from the indexed copy. None means freshness was not measured. 0 means current, and a larger value may mean "
        "a page is behind its file.",
    )


class SearchResult(BaseModel):
    """One `search` call's ranked hits plus the next-move block."""

    model_config = ConfigDict(frozen=True)

    hits: tuple[SearchHit, ...] = Field(default=(), description="The ranked hits, in score order.")
    status: SearchResultStatus = Field(
        default="unassessed",
        description="What this search actually found: no_hits, lexically_supported_candidates, semantic_candidates_only, or "
        "budget_refused. Every non-refused status still means unverified candidates. Fetch before relying on one. "
        "unassessed means support was not computed for this result.",
    )
    window: ResultWindow = Field(
        default_factory=ResultWindow,
        description="How complete this list of hits is: whether it was cut short of the underlying total, and why. "
        "Defaults to a complete, unrefused window.",
    )
    next_steps: NextSteps = Field(description="What to do with these hits: fetch one, or widen the query.")
    knowledge_base: KnowledgeBaseStamp | None = Field(
        default=None,
        description="The knowledge base that answered and how fresh its indexed copy is. Read changed_since before trusting a page. "
        "None only when the host attached no stamp.",
    )


class FetchResult(BaseModel):
    """One unit fetched by its id (the fetch function's result row)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="The unit's id, as resolved.")
    ref: str | None = Field(
        default=None,
        description="The exact collection-qualified handle to pass to fetch, set by the read tool. None on a row read straight "
        "from the database function.",
    )
    level: str = Field(
        description="This unit's granularity tier: how big the fetched unit is. It uses the same vocabulary as a search hit's `hit_level`."
    )
    title: str | None = Field(default=None, description="The unit's display title.")
    body: str | None = Field(default=None, description="The unit's full text, the source to read before relying on it.")
    knowledge_base: KnowledgeBaseStamp | None = Field(
        default=None,
        description="The knowledge base that answered and how fresh its indexed copy is. Read changed_since before trusting a page. "
        "None only when the host attached no stamp.",
    )


class GraphNode(BaseModel):
    """One node of the knowledge base graph, with its identity and lifecycle."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="The node's page id. It is fetchable.")
    ref: str | None = Field(
        default=None,
        description="The exact collection-qualified handle to pass to fetch. None only when the host attached no handle.",
    )
    type: str = Field(description="The node's kind in this knowledge base.")
    title: str = Field(description="The node's display title.")
    status: str = Field(description="The node's lifecycle status.")
    superseded: bool = Field(description="True when a newer page replaces this one. Prefer the successor it links to.")


class GraphEdge(BaseModel):
    """One typed relation between two graph nodes."""

    model_config = ConfigDict(frozen=True)

    src_id: str = Field(description="The node the relation points from.")
    dst_id: str = Field(description="The node the relation points to.")
    rel: str = Field(description="The relation's type.")
    provenance_claim: str | None = Field(
        default=None, description="The claim unit asserting this edge, when known. Fetch it to verify the relationship."
    )
    provenance_ref: str | None = Field(
        default=None,
        description="The exact collection-qualified handle for provenance_claim, and None whenever that is None.",
    )


class GraphReach(BaseModel):
    """How far a walk's seeds reach in the graph, read by a walk with no hop limit."""

    model_config = ConfigDict(frozen=True)

    reached_nodes: int = Field(description="How many nodes the seeds reach, the seeds included.")
    deepest_hop: int = Field(description="The hops to the farthest node reached.")
    target_hop: int | None = Field(
        default=None, description="The hops to the route's end. None when the walk did not reach it or had no end to reach."
    )
    complete: bool = Field(
        description="Whether the walk took in every node the seeds reach. False when it stopped at its node limit or at the route's end."
    )


class GraphResult(BaseModel):
    """The whole (or filtered) knowledge base graph: its nodes and the edges between surviving nodes."""

    model_config = ConfigDict(frozen=True)

    nodes: tuple[GraphNode, ...] = Field(default=(), description="The reached nodes, seeds included.")
    edges: tuple[GraphEdge, ...] = Field(default=(), description="The typed relations between the reached nodes.")
    window: ResultWindow = Field(
        default_factory=ResultWindow,
        description="How complete this subgraph is: whether nodes were cut short of the underlying traversal, and why. "
        "Defaults to a complete, unrefused window.",
    )
    reach: GraphReach | None = Field(
        default=None,
        description="How far the seeds reach with no hop limit, read when every reached node fits the answer. None when it was not read.",
    )
    knowledge_base: KnowledgeBaseStamp | None = Field(
        default=None,
        description="The knowledge base that answered and how fresh its indexed copy is. Read changed_since before trusting a node. "
        "None only when the host attached no stamp.",
    )
    next_steps: NextSteps | None = Field(
        default=None,
        description="What to do with this subgraph: fetch a node or a provenance claim, or widen the traversal. None only "
        "when the host attached no next-move block.",
    )


class GraphPath(BaseModel):
    """One ranked route between two nodes, the ordered nodes and edges along it, plus its `score`.

    The result element of `GraphPaths`: `nodes` are the nodes traversed (endpoints included) in
    order, `edges` the typed relations between consecutive nodes, and `score` ranks the path (for
    example by length or relation weight) so the top `k` can be selected.
    """

    model_config = ConfigDict(frozen=True)

    nodes: tuple[GraphNode, ...] = Field(description="The nodes along the route, endpoints included, in order.")
    edges: tuple[GraphEdge, ...] = Field(description="The typed relation between each consecutive node pair.")
    score: float = Field(description="Ranks this path against the others returned, higher first. Not a claim about the relation's importance.")


class GraphPathsResult(BaseModel):
    """One `find_paths` call's ranked routes between two nodes, bounded and stamped.

    A path's `score` orders it within this list. It says nothing about whether the relation chain is
    correct. Every edge is an authored assertion, so fetch its `provenance_claim` before relying on it.
    """

    model_config = ConfigDict(frozen=True)

    paths: tuple[GraphPath, ...] = Field(default=(), description="The ranked routes between the two endpoints, in score order.")
    next_steps: NextSteps = Field(description="What to do with these routes: fetch a provenance claim, or widen the search.")
    knowledge_base: KnowledgeBaseStamp | None = Field(
        default=None,
        description="The knowledge base that answered and how fresh its indexed copy is. Read changed_since before trusting a path. "
        "None only when the host attached no stamp.",
    )
    window: ResultWindow = Field(
        default_factory=ResultWindow,
        description="How complete this list of routes is: whether paths were cut short of the underlying total, and why. "
        "Defaults to a complete, unrefused window.",
    )
    reach: GraphReach | None = Field(
        default=None,
        description="How far the start reaches with no hop limit, read only when no route was found. None otherwise.",
    )


class ColumnInfo(BaseModel):
    """One column of a schema object: its name, SQL type, and comment (a schema-description line)."""

    model_config = ConfigDict(frozen=True)

    name: str
    data_type: str
    comment: str | None = None


class ObjectInfo(BaseModel):
    """One schema object (view, function, or table) with its comment and ordered columns."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: Literal["view", "function", "table"]
    comment: str | None = None
    columns: tuple[ColumnInfo, ...] = ()


class SchemaDescription(BaseModel):
    """The raw material for a schema description: every surface object with its comments and columns."""

    model_config = ConfigDict(frozen=True)

    objects: tuple[ObjectInfo, ...] = ()


class StatusReport(BaseModel):
    """Indexed-knowledge base health: per-table row counts plus the embed-model contract and last index.

    `row_counts` maps a table name to its row count. `embed_model`, `embed_dim`, and `embedded_at`
    are the single embed-metadata row (all `None` before the first index).
    """

    model_config = ConfigDict(frozen=True)

    row_counts: tuple[tuple[str, int], ...] = ()
    embed_model: str | None = None
    embed_dim: int | None = None
    embedded_at: str | None = None
