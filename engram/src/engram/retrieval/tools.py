r"""engram's retrieval tools over the shared retrieval toolset in `core.ai.retrieval.contracts`.

The shared handler Protocols, `RetrievalHandlers`, `RetrievalToolset` and the shared constants are
re-exported here, so callers reach the whole toolset through this one module.

This module adds the tool descriptions, the `next_steps` block every `search` and leveled `fetch`
result carries, and the builders that register the tools with engram's `Search` as the message
type. `build_retrieval_toolset` builds a pydantic-ai toolset, `register_mcp_tools` registers on a
FastMCP server, and `register_routed_mcp_tools` and `add_routed_read_tools` serve several
registered knowledge bases, routing each read by its collection.

`search_hits`, `search_result_status` and `bound_search_result` assemble a `search` answer. The
answer never exceeds `SEARCH_RESULT_CHARS`. Display text shortens before a hit is dropped, and an id
or a ref is never shortened.

No database access or other I/O happens here. The handlers own the gateway.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from core.ai.disclosure.contracts import DisclosureEnvelope
from core.ai.retrieval.contracts import (
    FETCH,
    FIND_PATHS,
    GRAPH_EXPAND,
    RUN_SQL,
    SEARCH,
    SEARCH_SNIPPET_CHARS,
    TOOL_MAX_RETRIES,
    TOOL_NAMES,
    KnowledgeBaseStamp,
    ResultWindow,
    RetrievalHandlers,
    RetrievalToolset,
    SearchResultStatus,
    SearchSupport,
    SupportsFetch,
    SupportsFindPaths,
    SupportsGraphExpand,
    SupportsSearch,
    fetch_one,
    resolve_description,
)
from core.ai.retrieval.contracts import build_retrieval_toolset as _build_retrieval_toolset
from core.ai.retrieval.contracts import fetch_unit as legacy_fetch
from core.ai.retrieval.contracts import register_mcp_tools as _register_mcp_tools
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field, create_model
from pydantic_ai.exceptions import ModelRetry

# These domain types are constructed at runtime by the tools below.
from .budget import fits_budget, longest_fitting, with_fallback_steps
from .domain import FetchResult, FoldedUnit, NextSteps, Period, Row, Search, SearchHit, SearchResult, period_end, period_start
from .envelope import EnvelopeHandler, fetchable_forms, normalize_ref, parse_ref, qualify_ref
from .envelope import fetch_description as leveled_fetch_description
from .exceptions import _REGROUND_WITH_SEARCH, BadRefError, RefRefusedError, WrongCollectionError
from .infrastructure.gateway import read_snapshot

# The search description takes its list of `hit_level` values from this schema.
from .knowledge_base_schema import KB_SEARCH_SCHEMA

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from core.ai.retrieval.contracts import StampSource
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.tools import RunContext, ToolDefinition

    from engram.indexing.contracts import RetrievalSwitches

    from .capability import EnvelopeQueries, ReadSurface

    Refuse = Callable[[str], Exception]
    """How the surface being registered on reports a refusal: `ToolError` on MCP, `ModelRetry` in a toolset."""

__all__ = [
    "DATE_RANGE_CLAUSE",
    "FETCH",
    "FIND_PATHS",
    "GRAPH_EXPAND",
    "RUN_SQL",
    "SEARCH",
    "SEARCH_RESULT_CHARS",
    "SEARCH_SNIPPET_CHARS",
    "TOOL_MAX_RETRIES",
    "TOOL_NAMES",
    "FetchEnvelope",
    "RefRefusal",
    "RetrievalHandlers",
    "RetrievalToolset",
    "SupportsFetch",
    "SupportsFindPaths",
    "SupportsGraphExpand",
    "SupportsSearch",
    "add_routed_read_tools",
    "bound_search_result",
    "build_retrieval_toolset",
    "declared_facets_clause",
    "document_clause",
    "elide_k",
    "envelope_next_steps",
    "fetch_description",
    "fetch_opens_map",
    "find_paths_description",
    "flat_fetch",
    "graph_expand_description",
    "leveled_fetch",
    "no_map_refusal",
    "no_unit_refusal",
    "refuse_duplicate_collections",
    "refuse_undeclared_facets",
    "register_mcp_tools",
    "register_routed_mcp_tools",
    "reground_phrase",
    "resolve_description",
    "search_hits",
    "search_next_steps",
    "search_ref_type",
    "search_result_status",
    "shared_hits_per_document",
]

# A short phrase for each known `hit_level` value, used in the search descriptions. A level absent
# from this map renders as its bare name.
_HIT_LEVEL_BLURBS: dict[str, str] = {
    "card": "a whole page",
    "section": "one section within a page",
    "claim": "a single asserted statement",
    "raw": "a raw source chunk",
}


def _hit_levels() -> tuple[str, ...]:
    """The `hit_level` values a search hit may carry, in the order of `KB_SEARCH_SCHEMA.surfaces`."""
    return tuple(surface.hit_level for surface in KB_SEARCH_SCHEMA.surfaces)


def _hit_level_clause(levels: tuple[str, ...]) -> str:
    """The `hit_level` legal-values clause a search description states, built from `levels`."""
    return "; ".join(f"`{level}` for {_HIT_LEVEL_BLURBS.get(level, level)}" for level in levels)


_HIT_LEVEL_CLAUSE = _hit_level_clause(_hit_levels())
"""The `hit_level` values the search surfaces produce, each with its phrase, as the search descriptions state them."""

# Tool descriptions are the agent's orientation, pydantic-ai and FastMCP both surface them to
# the model, so each names how to ground its ids and what its result carries.
_COLLECTION_CLAUSE = (
    "This database is shared across more than one collection. Set `collection` to search a "
    "different one instead of this knowledge base's own default, and `api.about` (via `run_sql`) lists "
    "what is here and what each one covers."
)
_COLLECTION_CLAUSE_NO_SQL = (
    "This database is shared across more than one collection. Set `collection` to search a "
    "different one instead of this knowledge base's own default."
)
"""The collection clause for a surface that serves no `run_sql`: it keeps the argument and drops the way to list the collections."""

DATE_RANGE_CLAUSE = (
    "`since` and `until` keep only pages whose page date falls in that range, both inclusive and applied before "
    "ranking. A page date is the day a page is about, read from the frontmatter field the knowledge base names for it, "
    "`created` (the day the page was written) unless it names another. Each takes a year (2026), a month (2026-09) or "
    "a day (2026-09-14), so `until='2026-09'` keeps all of September. A page with no page date never matches a range. "
    "A raw chunk is kept when a page made from its source falls in the range."
)
"""The sentences every `search` description spends on its date range."""

_DOCUMENT = "a source together with every page made from it, or a page made from no source"
"""What one document is, as a `search` description says it."""

_FOLDED = (
    "best first, each with a short `snippet` of the text it matched and the `ref` that fetches it, so `fetch` one "
    "directly rather than searching again."
)
"""How a `search` description says a hit's folded units are listed and read, where the surface serves `fetch`."""

_FOLDED_UNREAD = "best first, each with a short `snippet` of the text it matched and its `ref`."
"""How a `search` description says a hit's folded units are listed, where the surface serves no `fetch`."""

_SCORE_SENTENCE_END = "a relevance `score`."
"""The words every shipped `search` description ends its result sentence with, where the document sentences go."""


def document_clause(hits_per_document: int, *, can_fetch: bool = True) -> str:
    """What a `search` description says about documents under the cap on hits per document the database applies.

    Args:
        hits_per_document: The most hits one document keeps, as the knowledge base's search tuning sets it. 0 keeps every hit.
        can_fetch: Whether the surface serves `fetch`, which the sentences then send the agent to with a folded unit's ref.

    Returns:
        The sentences for a cap of 1 or more. An empty string for 0, since a search that keeps every hit says nothing
        about documents.
    """
    if hits_per_document <= 0:
        return ""
    folded = _FOLDED if can_fetch else _FOLDED_UNREAD
    if hits_per_document == 1:
        return f"Each hit is a distinct document: {_DOCUMENT}. A hit's `folded_units` lists more units of its document that matched, {folded}"
    return (
        f"At most {hits_per_document} hits come from one document: {_DOCUMENT}. The best hit of a document lists its next "
        f"matching units as `folded_units`, {folded}"
    )


def shared_hits_per_document(caps: Iterable[int]) -> int:
    """The cap on hits per document several knowledge bases share, 0 when they disagree or there are none.

    Args:
        caps: Each knowledge base's cap on hits per document.

    Returns:
        The one cap every knowledge base applies, or 0, so a description never states a cap one of them does not apply.
    """
    distinct = set(caps)
    return distinct.pop() if len(distinct) == 1 else 0


def _with_document_clause(description: str, hits_per_document: int, *, can_fetch: bool = True) -> str:
    """`description` with the document sentences for `hits_per_document` after its result sentence, or appended."""
    clause = document_clause(hits_per_document, can_fetch=can_fetch)
    if not clause:
        return description
    if _SCORE_SENTENCE_END in description:
        return description.replace(_SCORE_SENTENCE_END, f"{_SCORE_SENTENCE_END} {clause}", 1)
    return f"{description} {clause}"


def _facets_clause(facet_keys: tuple[str, ...]) -> str:
    """What a `search` description says about `facets`, naming the declared keys or saying there are none."""
    if not facet_keys:
        return "This knowledge base declares no facets, so leave `facets` out."
    return f"{_FACETS_MEANING} The facet keys this knowledge base declares: {_key_list(facet_keys)}. {_FACETS_VALUES}"


def _with_facets_clause(description: str, facets_clause: str) -> str:
    """A shipped search description with `facets_clause` after its date range sentences."""
    return description.replace(DATE_RANGE_CLAUSE, f"{DATE_RANGE_CLAUSE} {facets_clause}", 1)


_FACETS_MEANING = (
    "`facets` keeps only hits from pages carrying the given facet values, applied before ranking: map a facet key to a "
    "list of values, where any value of one key matches and every key given must match. A raw chunk is kept when a page "
    "made from its source matches."
)
"""The sentences every `search` description that takes `facets` spends on what a filter keeps."""

_FACETS_VALUES = "Write each value exactly as the knowledge base stores it."
"""The sentence every `search` description that takes `facets` ends its facets sentences with."""


def _key_list(facet_keys: tuple[str, ...]) -> str:
    """The facet keys as a description or a refusal names them."""
    return ", ".join(f"`{key}`" for key in facet_keys)


def _keys_by_base(declared: Mapping[str, tuple[str, ...]]) -> str:
    """Each knowledge base with its declared facet keys in parentheses, `none` for a base that declares none."""
    return ", ".join(f"{name!r} ({_key_list(keys) or 'none'})" for name, keys in declared.items())


def declared_facets_clause(declared: Mapping[str, tuple[str, ...]], *, several_per_call: bool) -> str:
    """The `facets` sentences of a `search` over one or several knowledge bases, naming each base's declared keys.

    With one knowledge base the text is the single-base search's own. With several it lists each base's keys
    and says which keys one call may use.

    Args:
        declared: Each knowledge base the search serves, by the name the search knows it by, mapped to the facet
            keys its schema declares, in the order the search lists them.
        several_per_call: Whether one call can search several knowledge bases. Such a call may use a key only
            when every base it searches declares it. Otherwise a call may use the keys of the base it searches.

    Returns:
        The sentences, which tell the agent to leave `facets` out when no base declares a key.
    """
    if len(declared) == 1:
        return _facets_clause(next(iter(declared.values())))
    if not any(declared.values()):
        return "No knowledge base here declares facets, so leave `facets` out."
    usable = (
        "A call may use a key only when every knowledge base it searches declares it."
        if several_per_call
        else "A call may use only the keys of the knowledge base it searches."
    )
    return f"{_FACETS_MEANING} The facet keys each knowledge base declares: {_keys_by_base(declared)}. {usable} {_FACETS_VALUES}"


def _served_clause(collection: str) -> str:
    """The collection sentence of a search that reads one collection only, naming it."""
    return f"This tool searches one collection, '{collection}': leave `collection` out, since any other value is refused."


def _served_description(description: str, collection: str) -> str:
    """A search description whose collection sentences say it reads `collection` alone."""
    text = description
    for clause in (_COLLECTION_CLAUSE, _COLLECTION_CLAUSE_NO_SQL):
        text = text.replace(clause, _served_clause(collection))
    return text


def _without_sql_plane(description: str) -> str:
    """A search description with every sentence that sends the agent to `run_sql` removed."""
    return description.replace(_COLLECTION_CLAUSE, _COLLECTION_CLAUSE_NO_SQL)


_FETCH_SENTENCES = (
    (" Start here to find candidate sources, then `fetch` a hit's full text by its `id`.", " Start here to find candidate sources."),
    (" Start here to find candidate sources, then `fetch` a hit's full text by `id`.", " Start here to find candidate sources."),
    ("Every hit stays a candidate until you fetch and read it.", "Every hit stays a candidate: its snippet is an excerpt, never the whole unit."),
    ("every hit stays a candidate until you fetch and read it.", "every hit stays a candidate: its snippet is an excerpt, never the whole unit."),
)
"""Each sentence a shipped `search` description sends the agent to `fetch` with, and how it reads where no `fetch` is served."""


def _without_fetch(description: str) -> str:
    """A search description whose sentences sending the agent to `fetch` read as they do on a surface serving none."""
    text = description
    for sentence, without in _FETCH_SENTENCES:
        text = text.replace(sentence, without)
    return text


def _pinned_description(description: str) -> str:
    """A search description without its collection sentences."""
    text = description
    for clause in (_COLLECTION_CLAUSE, _COLLECTION_CLAUSE_NO_SQL):
        text = text.replace(f" {clause}", "")
    return text


SEARCH_DESCRIPTION = (
    "Hybrid search over the knowledge base (semantic vector similarity + BM25 keyword matching, fused): "
    "returns the top-`k` ranked hits, each with its `id`, "
    f"`hit_level` ({_HIT_LEVEL_CLAUSE}), `page_id`/`page_title`, a `snippet`, and a relevance `score`. "
    "`query` is the search text (the server embeds it for the vector leg unless `bm25_only`). `k` "
    "caps the hits (default 12). Set `bm25_only=true` "
    f"for keyword-only ranking. {DATE_RANGE_CLAUSE} {_COLLECTION_CLAUSE} Start here to find candidate sources, then "
    "`fetch` a hit's full text by its `id`."
    " Read the result's `status` for what this search actually found, its `window` for whether "
    "lower-ranked hits were cut to fit the response budget, and each hit's `support` for which legs "
    "selected it: leg presence, never proof the hit answers your question. Every hit stays a "
    "candidate until you fetch and read it."
)
# Descriptions for a search whose leg is fixed by the host, so the tool takes no `bm25_only`.
SEARCH_DESCRIPTION_HYBRID = (
    "Hybrid search over the knowledge base, combining semantic vector similarity with BM25 keyword matching "
    "(RRF-fused), so a hit can rank on meaning, on exact terms, or both: distinctive exact tokens "
    "(names, acronyms, ids) in the `query` rank on the keyword leg while conceptual phrasing ranks "
    "on the vector leg: feed both. Returns the top-`k` ranked "
    "hits, each with its `id`, "
    f"`hit_level` ({_HIT_LEVEL_CLAUSE}), `page_id`/`page_title`, a `snippet`, and a relevance `score`. "
    f"`query` is the search text (the server embeds it for the vector leg). `k` caps the hits (default 12). {DATE_RANGE_CLAUSE} {_COLLECTION_CLAUSE} Start here to "
    "find candidate sources, then `fetch` a hit's full "
    "text by `id`."
    " Read the result's `status` for what this search actually found, its `window` for whether "
    "lower-ranked hits were cut to fit the response budget, and each hit's `support` for which legs "
    "selected it: leg presence, never proof the hit answers your question. Every hit stays a "
    "candidate until you fetch and read it."
)
SEARCH_DESCRIPTION_KEYWORD = (
    "Keyword search over the knowledge base (BM25 lexical matching only: no semantic vector leg), so it "
    "ranks on exact and overlapping terms, ideal for specific names, ids, or quoted phrasing: "
    "returns the top-`k` ranked hits, "
    f"each with its `id`, `hit_level` ({_HIT_LEVEL_CLAUSE}), `page_id`/`page_title`, a `snippet`, and a relevance `score`. "
    f"`query` is the search text (matched lexically). `k` caps the hits (default 12). {DATE_RANGE_CLAUSE} {_COLLECTION_CLAUSE} Start here to "
    "find candidate sources, then `fetch` a hit's full "
    "text by `id`."
    " Read the result's `status` for what this search actually found and its `window` for whether "
    "lower-ranked hits were cut to fit the response budget. A keyword match means your terms "
    "appeared, never that the hit answers your question: every hit stays a candidate until you "
    "fetch and read it."
)
def fetch_description(*, tools: frozenset[str] = TOOL_NAMES, collection: str | None = None) -> str:
    """The flat `fetch` tool's description, naming only tools the agent holds as the source of a `unit_id`.

    Without `search`, the description sends the agent to a prior result of the graph tools in `tools`,
    or to the task itself for an id.

    Args:
        tools: The read tools the surface registers. The default is all four.
        collection: The knowledge base the reads resolve within, named in the id forms. `None` names a stand-in.

    Returns:
        The full tool description text.
    """
    forms = fetchable_forms(collection)
    if SEARCH in tools:
        return (
            "Fetch one unit's full text by id: a whole page, a section within one, or a smaller unit. "
            f"`unit_id` names a unit a `search` hit returned. {forms} "
            "Returns the unit's `id`, `level` (the same vocabulary as a search hit's `hit_level`), "
            "`title`, and `body`. Use it to read the source behind a "
            "search hit before relying on it or citing it. An unknown id is reported so you can re-ground it "
            "with `search`."
        )
    graph = _graph_tools_named(tools)
    if graph is None:
        return (
            "Fetch one unit's full text by id: a whole page, a section within one, or a smaller unit. "
            f"`unit_id` names a unit supplied with the task. {forms} Returns the unit's `id`, `level` (a page, "
            "a section, or a smaller unit), `title`, and `body`. Use it to read a unit's full text before relying "
            "on it or citing it. An unknown id is reported so you can re-ground it with the ids supplied with the "
            "task and try again."
        )
    return (
        "Fetch one unit's full text by id: a whole page, a section within one, or a smaller unit. "
        f"`unit_id` names a node or a claim from a prior {graph} result, or a unit supplied "
        f"with the task. {forms} Returns the unit's `id`, `level` (a page, a section, or a smaller unit), "
        "`title`, and `body`. Use it to read a reached node's full text before relying on it or citing "
        f"it. An unknown id is reported so you can re-ground it with {graph} "
        "and try again."
    )


def _graph_tools_named(tools: frozenset[str]) -> str | None:
    """The graph tools in `tools` as text names them, or `None` when it holds neither."""
    named = [f"`{name}`" for name in (GRAPH_EXPAND, FIND_PATHS) if name in tools]
    return " or ".join(named) if named else None


def graph_expand_description(*, can_search: bool, can_fetch: bool) -> str:
    """The `graph_expand` tool's description, naming only tools the agent holds as the source of `seed_ids`.

    Without `search`, the description names the task as the source of the seed ids. With `fetch`,
    it names `fetch` as the way to read a reached node's full text.

    Args:
        can_search: Whether the resolved profile also exposes `search`.
        can_fetch: Whether the resolved profile also exposes `fetch`.

    Returns:
        The full tool description text.
    """
    fetch_clause = (
        " Fetch a reached node's full text with `fetch(unit_id=...)` before relying on what it connects to." if can_fetch else ""
    )
    if can_search:
        return (
            "Traverse the knowledge base knowledge graph outward from one or more seed pages and return the reached "
            "neighbourhood as `{nodes, edges}`. `seed_ids` are the starting page ids (ground them with a "
            "prior `search`): passing SEVERAL seeds in one call maps their combined neighbourhood, the "
            "efficient way to see how two things each relate to a shared family or component. `hops` is the "
            "traversal depth (default 2). Each edge is a typed relationship and carries its "
            "`provenance_claim` (the unit id that asserts it, when known). Use it to discover what a page "
            f"connects to (sources it references, depends on, or relates to) that a flat search may miss.{fetch_clause}"
        )
    return (
        "Traverse the knowledge base knowledge graph outward from one or more seed pages and return the reached "
        "neighbourhood as `{nodes, edges}`. `seed_ids` are the starting page ids. Use endpoint ids "
        "supplied with the task. Passing SEVERAL seeds in one call maps their combined neighbourhood, "
        "the efficient way to see how two things each relate to a shared family or component. `hops` "
        "is the traversal depth (default 2). Each edge is a typed relationship and carries its "
        "`provenance_claim` (the unit id that asserts it, when known). Use it to discover what a page "
        f"connects to: sources it references, depends on, or relates to.{fetch_clause}"
    )


def find_paths_description(*, can_search: bool, can_fetch: bool) -> str:
    """The `find_paths` tool's description, naming only tools the agent holds as the source of `from_id` and `to_id`.

    Without `search`, the description names the task as the source of the endpoint ids. With
    `fetch`, it names `fetch` as the way to verify a relation's claim.

    Args:
        can_search: Whether the resolved profile also exposes `search`.
        can_fetch: Whether the resolved profile also exposes `fetch`.

    Returns:
        The full tool description text.
    """
    fetch_clause = " Fetch the claim asserting a relation with `fetch(unit_id=...)` to verify it." if can_fetch else ""
    if can_search:
        return (
            "Find the connecting paths between two nodes in the knowledge base knowledge graph and return them ranked "
            "shortest first. The result carries a `window` saying whether routes were cut to fit the response "
            "budget and a freshness stamp for the knowledge base that answered. Each path is its ordered "
            "`nodes`, the typed `edges` between them (each with its `provenance_claim`), and a `score`. `from_id` / `to_id` are the endpoint ids (ground them with "
            "`search` first). Use it to explain HOW two items relate (the chain of relationships linking "
            "them) when the question is about a connection or influence. `max_hops` is the longest route to look for, 4 "
            "by default and at most 8. Paths follow edge DIRECTION, so an empty result means no directed chain from "
            "`from_id` to `to_id` within `max_hops` is recorded, not that the two are unrelated: raise `max_hops`, try "
            "the reverse direction, or `graph_expand` with both ids as seeds to surface "
            f"a shared neighbour (two siblings of one family have no path between them, but one expansion "
            f"shows both membership edges).{fetch_clause}"
        )
    return (
        "Find the connecting paths between two nodes in the knowledge base knowledge graph and return them ranked "
        "shortest first. The result carries a `window` saying whether routes were cut to fit the response "
        "budget and a freshness stamp for the knowledge base that answered. Each path is its ordered "
        "`nodes`, the typed `edges` between them (each with its `provenance_claim`), and a `score`. "
        "`from_id` / `to_id` are the endpoint ids. Use endpoint ids supplied with the task. "
        "Use it to explain HOW two items relate (the chain of relationships linking "
        "them) when the question is about a connection or influence. `max_hops` is the longest route to look for, 4 by "
        "default and at most 8. Paths follow edge DIRECTION, so an empty result means no directed chain from `from_id` "
        "to `to_id` within `max_hops` is recorded, not that the two are unrelated: raise `max_hops`, try the reverse "
        "direction, or `graph_expand` with both ids as seeds to surface "
        f"a shared neighbour (two siblings of one family have no path between them, but one expansion "
        f"shows both membership edges).{fetch_clause}"
    )


FETCH_DESCRIPTION = fetch_description()
GRAPH_EXPAND_DESCRIPTION = graph_expand_description(can_search=True, can_fetch=True)
FIND_PATHS_DESCRIPTION = find_paths_description(can_search=True, can_fetch=True)

# The six descriptions above are the default text of the retrieval tools with every tool present.
# The underscore names below are aliases of the same objects.
_SEARCH_DESCRIPTION = SEARCH_DESCRIPTION
_SEARCH_DESCRIPTION_HYBRID = SEARCH_DESCRIPTION_HYBRID
_SEARCH_DESCRIPTION_KEYWORD = SEARCH_DESCRIPTION_KEYWORD
_FETCH_DESCRIPTION = FETCH_DESCRIPTION
_GRAPH_EXPAND_DESCRIPTION = GRAPH_EXPAND_DESCRIPTION
_FIND_PATHS_DESCRIPTION = FIND_PATHS_DESCRIPTION


# -- the descriptions the builders register ------------


def search_description(
    *,
    hybrid: bool,
    can_sql: bool = True,
    facet_keys: tuple[str, ...] | None = None,
    takes_collection: bool = True,
    hits_per_document: int = 0,
) -> str:
    """The `search` description for a leg the host fixes rather than the agent.

    Args:
        hybrid: Whether this surface's search runs the vector leg beside BM25.
        can_sql: Whether the surface registering this `search` also serves `run_sql`. `True` (the
            default) keeps the sentence that lists the other collections through `api.about`.
            `False` drops it.
        facet_keys: The facet keys the knowledge base's schema declares, named in the `facets`
            sentences. `None` (the default) describes a search that takes no `facets`.
        takes_collection: Whether the tool takes a `collection` argument. `False` drops every
            sentence about it, for a search that has none.
        hits_per_document: The most hits one document keeps, as the knowledge base's search tuning sets it, which the
            description states. 0 (the default) keeps every hit and states nothing about documents.

    Returns:
        The full tool description text.
    """
    text = _with_document_clause(SEARCH_DESCRIPTION_HYBRID if hybrid else SEARCH_DESCRIPTION_KEYWORD, hits_per_document)
    if not can_sql:
        text = _without_sql_plane(text)
    if not takes_collection:
        text = _pinned_description(text)
    return text if facet_keys is None else _with_facets_clause(text, _facets_clause(facet_keys))


_K_CLAUSES = ((". `k` caps the hits (default 12)", ""), ("top-`k` ranked hits", "top ranked hits"))
"""What a search description says about `k`, and how it reads when the cap is not the agent's."""


def elide_k(description: str) -> str:
    """A search description with its `k` clauses removed, everything it teaches besides kept.

    Args:
        description: The search description as it reads when the agent argues its own cap.

    Returns:
        The same text without the sentences that offered `k`.
    """
    text = description
    for clause, without in _K_CLAUSES:
        text = text.replace(clause, without)
    return text


def _final_descriptions(
    *,
    search_hybrid: bool | None,
    descriptions: Mapping[str, str] | None,
    k_source: Callable[[], int] | None = None,
    selected: frozenset[str] = TOOL_NAMES,
    can_sql: bool = True,
    pinned: bool = False,
    facets_clause: str | None = None,
    collection: str | None = None,
    served: str | None = None,
    hits_per_document: int = 0,
) -> dict[str, str]:
    """Each tool's description after any override: `facets_clause` `None` takes no `facets`, `served` names the one collection searched."""
    can_search = SEARCH in selected
    can_fetch = FETCH in selected
    if search_hybrid is None:
        search_default = SEARCH_DESCRIPTION
    elif search_hybrid:
        search_default = SEARCH_DESCRIPTION_HYBRID
    else:
        search_default = SEARCH_DESCRIPTION_KEYWORD
    search_default = _with_document_clause(search_default, hits_per_document, can_fetch=can_fetch)
    if not can_fetch:
        search_default = _without_fetch(search_default)
    if not can_sql:
        search_default = _without_sql_plane(search_default)
    if facets_clause is not None:
        search_default = _with_facets_clause(search_default, facets_clause)
    search_text = resolve_description(SEARCH, search_default, descriptions)
    if pinned:
        search_text = _pinned_description(search_text)
    elif served is not None:
        search_text = _served_description(search_text, served)
    return {
        SEARCH: elide_k(search_text) if k_source is not None else search_text,
        FETCH: resolve_description(FETCH, fetch_description(tools=selected, collection=collection), descriptions),
        GRAPH_EXPAND: resolve_description(GRAPH_EXPAND, graph_expand_description(can_search=can_search, can_fetch=can_fetch), descriptions),
        FIND_PATHS: resolve_description(FIND_PATHS, find_paths_description(can_search=can_search, can_fetch=can_fetch), descriptions),
    }


def _resolve_search_hybrid(search_hybrid: bool | None, switches: RetrievalSwitches | None) -> bool | None:
    """`search_hybrid`, or `False` when it is `None` and the switches turn embeddings off."""
    if search_hybrid is not None or switches is None or switches.embeddings:
        return search_hybrid
    return False


def _switch_gated_selection(selected: frozenset[str], switches: RetrievalSwitches | None) -> frozenset[str]:
    """`selected` without the graph tools when the graph switch is off."""
    if switches is not None and not switches.graph:
        return selected - {GRAPH_EXPAND, FIND_PATHS}
    return selected


SEARCH_RESULT_CHARS = 12_000
"""The longest complete serialized `SearchResult` a search tool answers with, in characters.

Counted over the whole model: hits, refs, titles, snippets, status, window, next steps and stamp.
"""

_TRUNCATION_REASON = (
    "Lower-ranked hits were dropped to keep this answer inside its size budget. Search again with a narrower query to "
    "see fewer hits in full."
)
"""The reason a short window states: what was cut, and the narrower call that avoids the cut."""

_REFUSAL_REASON = "The top hit's id and ref alone exceed this answer's size budget, so no hit could be returned intact."
"""The reason a refused window states when the top hit's id and ref alone exceed the budget."""

_REFUSAL_STEPS = NextSteps(
    summary="No hit fits this answer's size budget, so none was returned.",
    options=("Search again with narrower, more specific terms: a tighter query reaches smaller units.",),
)
"""The fixed next move a budget refusal offers. It never repeats the identity that overflowed."""

_MINIMAL_STEPS = NextSteps(
    summary="This answer's own next-move block did not fit its size budget, so this short one replaced it.",
    options=("Search again with narrower, more specific terms.",),
)
"""The next move that replaces a composed block that pushed the answer past its budget."""

_UNVERIFIED_CLAUSE = "Candidates are unverified evidence until you fetch and read them."
"""What a candidate list is, said on every result that carries hits where `fetch` is served."""

_UNVERIFIED_UNREAD_CLAUSE = "Candidates are unverified evidence: each snippet is an excerpt, never the whole unit."
"""What a candidate list is, said on every result that carries hits where no `fetch` is served."""


def _clip(text: str | None, limit: int) -> str | None:
    """`text` cut to at most `limit` characters, ending in an ellipsis when anything was removed."""
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026" if limit > 0 else ""


def _search_support(lexical_rank: int | None, vector_rank: int | None) -> SearchSupport:
    """Which legs selected a hit, read from rank presence alone. Call it only when at least one rank is present."""
    if lexical_rank is not None:
        return "lexical_and_semantic" if vector_rank is not None else "lexical_only"
    return "semantic_only"


def search_result_status(hits: tuple[SearchHit, ...]) -> SearchResultStatus:
    """What a search found, read from its hits' own support labels.

    No hits is `no_hits`. One hit with keyword support makes the whole result
    `lexically_supported_candidates`. Hits the semantic leg alone selected make it
    `semantic_candidates_only`. Hits from a database that publishes no component ranks are
    `unassessed`.

    Args:
        hits: The projected hits, in rank order.

    Returns:
        The status the result carries.
    """
    if not hits:
        return "no_hits"
    supported = tuple(hit.support for hit in hits if hit.support is not None)
    if not supported:
        return "unassessed"
    if any(support != "semantic_only" for support in supported):
        return "lexically_supported_candidates"
    return "semantic_candidates_only"


def search_hits(rows: tuple[Row, ...], *, collection: str, knowledge_base: str, hybrid: bool) -> tuple[SearchHit, ...]:
    """Project search rows into hits carrying their fetchable ref, their support, a bounded snippet and their folded units.

    `answered_by` follows the legs that actually selected each hit: the semantic leg contributing
    makes it `hybrid`, its absence makes it `lexical`. A row from a database that publishes no
    component ranks has no per-leg facts to read, so it keeps the leg this call ran and no support
    label. Snippets are bounded here as well as in the generated SQL, so a hit is an excerpt
    whichever database answered. Each folded unit carries the ref a hit of its level would carry, and its snippet
    bounded as a hit's.

    Args:
        rows: The search handler's rows, in rank order.
        collection: The partition whose ids these are, qualifying every ref.
        knowledge_base: What a caller names this knowledge base by.
        hybrid: Whether this call ran the vector leg at all.

    Returns:
        The projected hits, in the rows' own order.
    """
    projected: list[SearchHit] = []
    for row in rows:
        hit = SearchHit.model_validate(dict(row))
        ranked = hit.lexical_rank is not None or hit.vector_rank is not None
        support = _search_support(hit.lexical_rank, hit.vector_rank) if ranked else None
        selected_semantically = hit.vector_rank is not None if ranked else hybrid
        leg: Literal["lexical", "hybrid"] = "hybrid" if selected_semantically else "lexical"
        snippet = _clip(hit.snippet, SEARCH_SNIPPET_CHARS)
        folded = tuple(
            unit.model_copy(
                update={
                    "ref": qualify_ref(f"{search_ref_type(unit.hit_level)}:{unit.id}", collection),
                    "snippet": _clip(unit.snippet, SEARCH_SNIPPET_CHARS),
                }
            )
            for unit in hit.folded_units
        )
        projected.append(
            hit.model_copy(
                update={
                    "answered_by": leg,
                    "knowledge_base": knowledge_base,
                    "ref": qualify_ref(f"{search_ref_type(hit.hit_level)}:{hit.id}", collection),
                    "support": support,
                    "snippet": snippet,
                    "snippet_truncated": hit.snippet_truncated or snippet != hit.snippet,
                    "folded_units": folded,
                }
            )
        )
    return tuple(projected)


def _windowed(
    hits: tuple[SearchHit, ...],
    *,
    status: SearchResultStatus,
    next_steps: NextSteps,
    knowledge_base: KnowledgeBaseStamp | None,
    retrieved: int,
) -> SearchResult:
    """The result `hits` would make if nothing further were added: the window says what was left out."""
    omitted = retrieved - len(hits)
    return SearchResult(
        hits=hits,
        status=status,
        window=ResultWindow(
            returned_items=len(hits), omitted_items=omitted, truncated=omitted > 0, reason=_TRUNCATION_REASON if omitted else None
        ),
        next_steps=next_steps,
        knowledge_base=knowledge_base,
    )


def _shortened(hit: SearchHit, limit: int) -> SearchHit:
    """`hit` with only its display text bounded to `limit`: the id, the ref and the score are untouched."""
    snippet = _clip(hit.snippet, limit)
    return hit.model_copy(
        update={"page_title": _clip(hit.page_title, limit), "snippet": snippet, "snippet_truncated": hit.snippet_truncated or snippet != hit.snippet}
    )


def _fitted(
    kept: tuple[SearchHit, ...],
    candidate: SearchHit,
    *,
    status: SearchResultStatus,
    next_steps: NextSteps,
    knowledge_base: KnowledgeBaseStamp | None,
    retrieved: int,
    max_chars: int,
) -> SearchHit | None:
    """The longest form of `candidate` that keeps the whole result within `max_chars`, or `None` when none fits."""

    def fits(hit: SearchHit) -> bool:
        result = _windowed((*kept, hit), status=status, next_steps=next_steps, knowledge_base=knowledge_base, retrieved=retrieved)
        return fits_budget(result, max_chars)

    return longest_fitting(candidate, shorten=_shortened, fits=fits, longest=max(len(candidate.page_title or ""), len(candidate.snippet or "")))


def _folded_shortened(unit: FoldedUnit, limit: int) -> FoldedUnit:
    """`unit` with its snippet bounded to `limit`, or none below room for one character and the ellipsis."""
    return unit.model_copy(update={"snippet": _clip(unit.snippet, limit) if limit >= _SHORTEST_FOLDED_SNIPPET else None})


_SHORTEST_FOLDED_SNIPPET = 2
"""The shortest folded snippet a response budget leaves: one character of text and the ellipsis."""


def _with_last_folded_unit_fitted(
    hits: tuple[SearchHit, ...], position: int, fits: Callable[[tuple[SearchHit, ...]], bool]
) -> SearchHit | None:
    """The hit at `position` with its last folded unit's snippet as long as `fits` allows, or `None` when even none fits."""
    hit = hits[position]
    *kept, last = hit.folded_units

    def placed(unit: FoldedUnit) -> SearchHit:
        return hit.model_copy(update={"folded_units": (*kept, unit)})

    def fits_placed(unit: FoldedUnit) -> bool:
        return fits((*hits[:position], placed(unit), *hits[position + 1 :]))

    fitted = longest_fitting(last, shorten=_folded_shortened, fits=fits_placed, longest=len(last.snippet or ""))
    return placed(fitted) if fitted is not None else None


def _without_folded_units_past_the_budget(
    hits: tuple[SearchHit, ...],
    *,
    status: SearchResultStatus,
    next_steps: NextSteps,
    knowledge_base: KnowledgeBaseStamp | None,
    max_chars: int,
) -> tuple[SearchHit, ...]:
    """`hits` with folded units cut to the longest snippet that fits, else dropped, from the lowest-ranked hit's last one up."""

    def fits_whole(candidate: tuple[SearchHit, ...]) -> bool:
        whole = _windowed(candidate, status=status, next_steps=next_steps, knowledge_base=knowledge_base, retrieved=len(hits))
        return fits_budget(whole, max_chars)

    trimmed = hits
    for position in range(len(trimmed) - 1, -1, -1):
        while trimmed[position].folded_units:
            fitted = _with_last_folded_unit_fitted(trimmed, position, fits_whole)
            if fitted is not None:
                return (*trimmed[:position], fitted, *trimmed[position + 1 :])
            dropped = trimmed[position].model_copy(update={"folded_units": trimmed[position].folded_units[:-1]})
            trimmed = (*trimmed[:position], dropped, *trimmed[position + 1 :])
    return trimmed


def bound_search_result(
    hits: tuple[SearchHit, ...],
    *,
    status: SearchResultStatus,
    next_steps: NextSteps,
    knowledge_base: KnowledgeBaseStamp | None,
    max_chars: int = SEARCH_RESULT_CHARS,
) -> SearchResult:
    """The largest prefix of `hits` whose complete serialized result fits `max_chars`.

    Rank order is kept and a hit's id and ref are never shortened. When the hits do not all fit whole, folded units go
    first, one at a time from the lowest-ranked hit's last one up, each cut to a shorter snippet when that fits and
    dropped when it does not, before any hit's title or snippet is cut. Then a hit is
    added whole when it fits, else with its title and snippet bounded, else not at all, and then every later hit is
    left out too. The window counts what was left out. When not even the top hit's id and ref fit, the answer is an
    empty `budget_refused` result with a fixed next move.

    Args:
        hits: The projected hits, in rank order.
        status: What the search found, read from the hits it retrieved.
        next_steps: The next move these hits offer, naming the top hit's own ref.
        knowledge_base: The stamp of the knowledge base that answered, or `None` when unstamped.
        max_chars: The longest complete serialized result to answer with.

    Returns:
        The bounded result, its window saying how much of the retrieved list it carries.
    """
    hits = _without_folded_units_past_the_budget(hits, status=status, next_steps=next_steps, knowledge_base=knowledge_base, max_chars=max_chars)
    kept: tuple[SearchHit, ...] = ()
    for candidate in hits:
        fitted = _fitted(
            kept, candidate, status=status, next_steps=next_steps, knowledge_base=knowledge_base, retrieved=len(hits), max_chars=max_chars
        )
        if fitted is None:
            break
        kept = (*kept, fitted)
    if hits and not kept:
        refused = SearchResult(
            hits=(),
            status="budget_refused",
            window=ResultWindow(returned_items=0, omitted_items=len(hits), truncated=True, refused=True, reason=_REFUSAL_REASON),
            next_steps=_REFUSAL_STEPS,
            knowledge_base=knowledge_base,
        )
        return with_fallback_steps(refused, max_chars, steps=_MINIMAL_STEPS)
    windowed = _windowed(kept, status=status, next_steps=next_steps, knowledge_base=knowledge_base, retrieved=len(hits))
    return with_fallback_steps(windowed, max_chars, steps=_MINIMAL_STEPS)


def search_next_steps(
    hits: tuple[SearchHit, ...],
    *,
    query: str,
    knowledge_base: str,
    hybrid: bool,
    status: SearchResultStatus,
    map_collection: str | None,
    dated: bool = False,
    filtered: bool = False,
    can_fetch: bool = True,
) -> NextSteps:
    """The next-move block a `search` result carries: fetch a hit, or widen when there are none.

    The top hit is named by its exact fetchable `ref`, so the option goes through `fetch` where the surface serves it.
    Where it serves none, no option or summary names `fetch`, the hits are judged by their snippets and no map is
    offered. With no hits, the options are a different query and, when `map_collection` names one, the knowledge
    base's map. Every summary ends by naming which leg answered. When the semantic leg alone selected the hits, the
    summary says so and offers saying the knowledge base has no support for the question.

    Args:
        hits: The projected hits, in rank order.
        query: The caller's search text, quoted back bounded.
        knowledge_base: What to call the answering knowledge base in prose.
        hybrid: Whether the vector leg ran, named as the honesty line.
        status: What the search found, which decides whether the low-support wording applies.
        map_collection: The partition whose `collection:` map ref the browse option offers. `None`
            when no single partition answered or when the `fetch` beside this search opens no map,
            which drops the option rather than naming a ref that would not read back.
        dated: Whether a date range narrowed the search. With no hits, the widen option then names
            the range as a lever.
        filtered: Whether `facets` narrowed the search. With no hits, the widen option then names them
            as a lever.
        can_fetch: Whether the surface serves `fetch`. `False` names no `fetch` anywhere in the block.

    Returns:
        The next-move block.
    """
    leg = "hybrid" if hybrid else "lexical"
    # The query is bounded like any other display text before it is quoted back.
    echoed = query if len(query) <= SEARCH_SNIPPET_CHARS else f"{query[: SEARCH_SNIPPET_CHARS - 1]}\u2026"
    browse = None if map_collection is None or not can_fetch else f"fetch(unit_id='collection:{map_collection}')"
    if not hits:
        widen = _widen_option(dated=dated, filtered=filtered)
        return NextSteps(
            summary=f"No hits for {echoed!r} in the '{knowledge_base}' knowledge_base. Answered by {leg}.",
            options=(widen,) if browse is None else (widen, f"{browse} to open the map and browse by structure instead."),
        )
    top = hits[0]
    read_it = (
        f"fetch(unit_id={top.ref!r}) to read its full text: a hit is an address plus a snippet, never the source."
        if can_fetch
        else "Judge each hit by its snippet, an excerpt and never the source, and search again with narrower terms to bring "
        "a different passage into view."
    )
    sources = "the sources you fetch" if can_fetch else "the snippets"
    if status == "semantic_candidates_only":
        retry = "Re-run with the exact terms you expect" if browse is None else f"Re-run with the exact terms you expect, or browse by structure: {browse}"
        return NextSteps(
            summary=(
                f"{len(hits)} hit(s) for {echoed!r}: the nearest semantic candidates, matched on meaning with no keyword "
                f"match behind any of them, so this is evidence unverified. Answered by {leg}."
            ),
            options=(
                read_it,
                f"{retry}.",
                f"Say this knowledge base has no support for the question if {sources} do not answer it.",
            ),
        )
    return NextSteps(
        summary=f"{len(hits)} hit(s) for {echoed!r}, ranked by relevance. {_UNVERIFIED_CLAUSE if can_fetch else _UNVERIFIED_UNREAD_CLAUSE} "
        f"Answered by {leg}.",
        options=(read_it,),
    )


def _widen_option(*, dated: bool, filtered: bool) -> str:
    """The option a search with no hits offers, naming the date range and the facets only when the call passed them."""
    if filtered:
        if dated:
            return (
                "Try broader or different terms, widen or drop `since` and `until`, or loosen or drop `facets`: "
                "a page with no page date never matches a range."
            )
        return "Try broader or different terms, or loosen or drop `facets`."
    if dated:
        return "Try broader or different terms, or widen or drop `since` and `until`: a page with no page date never matches a range."
    return "Try broader or different terms."


def _refuse_undeclared_facets(
    facets: Mapping[str, list[str]], facet_keys: tuple[str, ...], refuse: Refuse, *, owner: str = "this knowledge base"
) -> None:
    """Raise through `refuse` when `facets` names a key `owner` does not declare."""
    undeclared = ", ".join(repr(key) for key in facets if key not in facet_keys)
    if not undeclared:
        return
    if not facet_keys:
        msg = f"{owner[:1].upper()}{owner[1:]} declares no facets, so `facets` takes no key (given: {undeclared}). Search again without `facets`."
    else:
        msg = (
            f"Not a facet key {owner} declares: {undeclared}. The declared keys are {_key_list(facet_keys)}. "
            "Search again with those keys, or without `facets`."
        )
    raise refuse(msg)


def refuse_undeclared_facets(facets: Mapping[str, list[str]], declared: Mapping[str, tuple[str, ...]], refuse: Refuse) -> None:
    """Refuse a `facets` filter naming a key that some knowledge base the call searches does not declare.

    One searched knowledge base refuses a key it does not declare, naming its keys. Several searched
    knowledge bases refuse a key unless every one declares it, naming each base's keys and asking the
    agent to name one knowledge base. A filter that passes is therefore declared by every searched base.

    Args:
        facets: The facet filter the call passed, a facet key mapped to its values.
        declared: Each knowledge base the call searches, by the name the search knows it by, mapped to the
            facet keys its schema declares.
        refuse: Builds the error a refusal raises: `ModelRetry` in a toolset, `ToolError` on MCP.

    Raises:
        Exception: The error `refuse` builds, raised when `facets` names a refused key.
    """
    if len(declared) == 1:
        ((name, facet_keys),) = declared.items()
        _refuse_undeclared_facets(facets, facet_keys, refuse, owner=_named_base(name))
        return
    unshared = ", ".join(repr(key) for key in facets if any(key not in facet_keys for facet_keys in declared.values()))
    if unshared:
        msg = (
            f"Not a facet key every searched knowledge base declares: {unshared}. The keys each one declares: "
            f"{_keys_by_base(declared)}. Name one knowledge base to filter by its own keys, or search again without `facets`."
        )
        raise refuse(msg)


def _named_base(name: str) -> str:
    """How a facet refusal names one knowledge base."""
    return f"the {name!r} knowledge base"


async def _run_search(
    handlers: RetrievalHandlers[Search],
    query: str,
    k: int,
    *,
    bm25_only: bool,
    search_type: type[Search],
    default_collection: str,
    collection: str | None = None,
    stamp: StampSource | None = None,
    since: str | None = None,
    until: str | None = None,
    pinned: bool = False,
    facets: Mapping[str, list[str]] | None = None,
    facet_keys: tuple[str, ...] = (),
    facet_owner: str = "this knowledge base",
    refuse: Refuse = ModelRetry,
    browsable: bool = True,
    can_fetch: bool = True,
) -> SearchResult:
    """Run one search into a bounded result: `pinned` ignores a passed collection, otherwise one unlike `default_collection` is refused."""
    if pinned:
        collection = None
    if collection is not None and collection != default_collection:
        raise refuse(_foreign_collection_refusal(default_collection))
    if facets:
        _refuse_undeclared_facets(facets, facet_keys, refuse, owner=facet_owner)
    scoped = default_collection
    hybrid = not bm25_only
    message = search_type(
        query=query,
        k=k,
        hybrid=hybrid,
        collection=scoped,
        since=None if since is None else period_start(since),
        until=None if until is None else period_end(until),
        facets=dict(facets or {}),
    )
    async with read_snapshot():
        result = await handlers.search(message)
        knowledge_base = await stamp() if stamp is not None else None
    hits = search_hits(result.rows, collection=scoped, knowledge_base=scoped, hybrid=hybrid)
    status = search_result_status(hits)
    return bound_search_result(
        hits,
        status=status,
        next_steps=search_next_steps(
            hits,
            query=query,
            knowledge_base=scoped,
            hybrid=hybrid,
            status=status,
            map_collection=scoped if browsable else None,
            dated=message.is_dated,
            filtered=bool(message.facets),
            can_fetch=can_fetch,
        ),
        knowledge_base=knowledge_base,
    )


def _foreign_collection_refusal(collection: str) -> str:
    """The refusal a single-source search gives a `collection` other than the one it reads."""
    return f"This search reads only the {collection!r} knowledge base, so `collection` takes no other value. Search again without `collection`."


def search_ref_type(hit_level: str) -> Literal["page", "section", "unit"]:
    """Map a search hit's `hit_level` onto the ref kind an agent fetches.

    Args:
        hit_level: The hit's level: `card`, `section`, `claim` or `raw`.

    Returns:
        `page` for a card, `section` for a section, and `unit` for a claim or a raw chunk.

    Raises:
        ValueError: When `hit_level` is none of the four.
    """
    if hit_level == "card":
        return "page"
    if hit_level == "section":
        return "section"
    if hit_level in {"claim", "raw"}:
        return "unit"
    msg = f"Unknown search hit level {hit_level!r}. Re-ground with `search` and try again."
    raise ValueError(msg)


def _build_search_fn(
    handlers: RetrievalHandlers[Search],
    *,
    k_source: Callable[[], int] | None,
    search_hybrid: bool | None,
    search_type: type[Search],
    default_collection: str,
    stamp: StampSource | None = None,
    pinned: bool = False,
    facet_keys: tuple[str, ...] = (),
    refuse: Refuse = ModelRetry,
    browsable: bool = True,
    can_fetch: bool = True,
) -> Callable[..., Awaitable[SearchResult]]:
    """Build the `search` closure: `k_source` drops `k`, `search_hybrid` drops `bm25_only`, `pinned` ignores `collection`, `browsable` offers the map."""

    async def run(
        query: str,
        k: int,
        *,
        bm25_only: bool,
        collection: str | None,
        since: str | None,
        until: str | None,
        facets: dict[str, list[str]] | None,
    ) -> SearchResult:
        return await _run_search(
            handlers,
            query,
            k,
            bm25_only=bm25_only,
            search_type=search_type,
            default_collection=default_collection,
            collection=collection,
            stamp=stamp,
            since=since,
            until=until,
            pinned=pinned,
            facets=facets,
            facet_keys=facet_keys,
            refuse=refuse,
            browsable=browsable,
            can_fetch=can_fetch,
        )

    if search_hybrid is None:
        if k_source is None:

            async def search_agent_choice(
                query: str,
                k: int = 12,
                bm25_only: bool = False,
                collection: str | None = None,
                *,
                since: Period | None = None,
                until: Period | None = None,
                facets: dict[str, list[str]] | None = None,
            ) -> SearchResult:
                return await run(query, k, bm25_only=bm25_only, collection=collection, since=since, until=until, facets=facets)

            return search_agent_choice

        async def search_agent_choice_capped(
            query: str,
            bm25_only: bool = False,
            collection: str | None = None,
            *,
            since: Period | None = None,
            until: Period | None = None,
            facets: dict[str, list[str]] | None = None,
        ) -> SearchResult:
            return await run(query, k_source(), bm25_only=bm25_only, collection=collection, since=since, until=until, facets=facets)

        return search_agent_choice_capped

    forced_bm25 = search_hybrid is False

    if k_source is None:

        async def search_forced(
            query: str,
            k: int = 12,
            collection: str | None = None,
            *,
            since: Period | None = None,
            until: Period | None = None,
            facets: dict[str, list[str]] | None = None,
        ) -> SearchResult:
            return await run(query, k, bm25_only=forced_bm25, collection=collection, since=since, until=until, facets=facets)

        return search_forced

    async def search_forced_capped(
        query: str,
        collection: str | None = None,
        *,
        since: Period | None = None,
        until: Period | None = None,
        facets: dict[str, list[str]] | None = None,
    ) -> SearchResult:
        return await run(query, k_source(), bm25_only=forced_bm25, collection=collection, since=since, until=until, facets=facets)

    return search_forced_capped


def _add_search_tool(
    toolset: RetrievalToolset,
    handlers: RetrievalHandlers[Search],
    *,
    k_source: Callable[[], int] | None,
    search_hybrid: bool | None,
    description: str,
    search_type: type[Search],
    default_collection: str,
    stamp: StampSource | None = None,
    pinned: bool = False,
    facet_keys: tuple[str, ...] = (),
    browsable: bool = True,
    can_fetch: bool = True,
) -> None:
    """Register engram's own `search` on the pydantic-ai `toolset`, a pinned one offering no collection (see `_build_search_fn`)."""
    search = _build_search_fn(
        handlers,
        k_source=k_source,
        search_hybrid=search_hybrid,
        search_type=search_type,
        default_collection=default_collection,
        stamp=stamp,
        pinned=pinned,
        facet_keys=facet_keys,
        browsable=browsable,
        can_fetch=can_fetch,
    )
    toolset.add_function(
        search,
        takes_ctx=False,
        name=SEARCH,
        description=description,
        retries=TOOL_MAX_RETRIES,
        prepare=_without_collection if pinned else None,
    )


_COLLECTION_ARGUMENT = "collection"
"""The `search` argument a pinned search never offers: which collection it reads."""


def _without_collection(_ctx: RunContext[object], tool_def: ToolDefinition) -> ToolDefinition:
    """The `search` definition with `collection` removed from its parameter schema."""
    schema = tool_def.parameters_json_schema
    properties = {name: argument for name, argument in schema["properties"].items() if name != _COLLECTION_ARGUMENT}
    return replace(tool_def, parameters_json_schema={**schema, "properties": properties})


def _add_search_mcp_tool(
    server: FastMCP,
    handlers: RetrievalHandlers[Search],
    *,
    k_source: Callable[[], int] | None,
    search_hybrid: bool | None,
    description: str,
    search_type: type[Search],
    default_collection: str,
    stamp: StampSource | None = None,
    facet_keys: tuple[str, ...] = (),
    browsable: bool = True,
    can_fetch: bool = True,
) -> None:
    """Register engram's own `search` on the FastMCP `server`, refusing through `ToolError` (see `_build_search_fn`)."""
    search = _build_search_fn(
        handlers,
        k_source=k_source,
        search_hybrid=search_hybrid,
        search_type=search_type,
        default_collection=default_collection,
        stamp=stamp,
        facet_keys=facet_keys,
        refuse=ToolError,
        browsable=browsable,
        can_fetch=can_fetch,
    )
    server.add_tool(search, name=SEARCH, description=description)


def _scoped_search_type(collection: str) -> type[Search]:
    """A `Search` subclass whose `collection` defaults to `collection`."""
    return create_model("CollectionScopedSearch", __base__=Search, collection=(str | None, collection))


class FetchEnvelope(DisclosureEnvelope):
    """A leveled `fetch` result: a `DisclosureEnvelope` that also carries its next-move block.

    It is a `DisclosureEnvelope`, so the envelope token budget still meters it, and `approx_tokens`
    counts `next_steps` too.
    """

    next_steps: NextSteps
    knowledge_base: KnowledgeBaseStamp | None = Field(
        default=None,
        description="The knowledge base that answered and how fresh its indexed copy is. Read changed_since before trusting a page. "
        "None only when the host attached no stamp.",
    )


class FetchUnit(FetchResult):
    """The flat `fetch` result for a claim or a raw chunk, with its next-move block."""

    next_steps: NextSteps


def envelope_next_steps(envelope: DisclosureEnvelope, collection: str) -> NextSteps:
    """The next-move block a disclosed unit carries, chosen by its ref's structural level.

    The level is read from the ref's prefix, never from `page_type`. A listing (`collection:` or
    `group:`) names its first real child. A section names its parent page and `graph_expand`. A page
    names its first section, when it has one, and `graph_expand`.

    Args:
        envelope: The disclosed unit.
        collection: The collection its ref resolves within.

    Returns:
        The next-move block.
    """
    real_children = tuple(child for child in envelope.children if child.kind != "truncated")
    resolved = parse_ref(envelope.ref, collection)
    if resolved.kind in {"collection", "group"}:
        if not real_children:
            return NextSteps(
                summary=f"'{envelope.title}' lists nothing here.",
                options=(f"search(query=...) to find units in the '{collection}' knowledge base instead.",),
            )
        first = real_children[0]
        return NextSteps(
            summary=f"{len(real_children)} item(s) listed under '{envelope.title}'.",
            options=(f"fetch(unit_id={first.ref!r}) to open '{first.title}'.",),
        )
    if resolved.kind == "section":
        parent = resolved.unit_id.partition("#")[0]
        parent_ref = qualify_ref(f"page:{parent}", resolved.collection)
        return NextSteps(
            summary=f"Section '{envelope.title}': the lowest level, with no further children.",
            options=(
                f"fetch(unit_id={parent_ref!r}) to read the whole page this section belongs to.",
                f"graph_expand(seed_ids=['{parent}']) to see what that page connects to.",
            ),
        )
    page_id = resolved.unit_id
    if not real_children:
        return NextSteps(
            summary=f"Page '{envelope.title}' has no listed sections.",
            options=(f"graph_expand(seed_ids=['{page_id}']) to see what this page connects to.",),
        )
    first = real_children[0]
    return NextSteps(
        summary=f"Page '{envelope.title}' with {len(real_children)} section(s).",
        options=(
            f"fetch(unit_id={first.ref!r}) to read the '{first.title}' section.",
            f"graph_expand(seed_ids=['{page_id}']) to see what this page connects to.",
        ),
    )


def _flat_fetch_next_steps(unit: FetchResult) -> NextSteps:
    """The next-move block a flat fetch result carries, which has no children to open."""
    return NextSteps(
        summary=f"Fetched '{unit.id}' ({unit.level}): no leveled surface for this id.",
        options=("search(query=...) to find related units. graph_expand/find_paths if it sits in the page graph.",),
    )


def reground_phrase(tools: frozenset[str]) -> str:
    """How a `fetch` refusal tells the agent to find a good id, naming only tools in `tools`.

    Args:
        tools: The read tools the surface registers.

    Returns:
        `search` when it is registered, else the graph tools it holds and the task's ids, else the
        task's ids alone.
    """
    if SEARCH in tools:
        return _REGROUND_WITH_SEARCH
    graph = _graph_tools_named(tools)
    return "the ids supplied with the task" if graph is None else f"{graph}, or the ids supplied with the task"


def no_unit_refusal(unit_id: str, collection: str | None, *, named: str = "", reground: str = _REGROUND_WITH_SEARCH) -> str:
    """The refusal for an id that names no unit, naming every id form `fetch` takes.

    Args:
        unit_id: The id that named nothing.
        collection: The knowledge base the forms name. `None` names a stand-in, for a reader over several.
        named: Text naming the knowledge base that was asked, such as ` in 'notes'`, or empty.
        reground: How the refusal tells the agent to find a good id (`reground_phrase`).

    Returns:
        The refusal text.
    """
    return f"No unit found with id {unit_id!r}{named}. {fetchable_forms(collection)} Re-ground the id with {reground} and try again."


def no_map_refusal(reground: str = _REGROUND_WITH_SEARCH) -> str:
    """The refusal a flat read gives a map request, which it has no level to list.

    Args:
        reground: How the refusal tells the agent to find a unit instead (`reground_phrase`).

    Returns:
        The refusal text.
    """
    return f"There is no browsable map here: find units with {reground}, then fetch one by its ref."


class RefRefusal(ModelRetry):
    """A read refused over the ref itself, its grammar or the knowledge base it names, rather than a missing unit.

    A reader trying several knowledge bases in turn reports this reason rather than its own "no unit found".
    """


def flat_fetch(
    handler: SupportsFetch,
    collection: str,
    *,
    stamp: StampSource | None = None,
    reground: str = _REGROUND_WITH_SEARCH,
    refuse: Refuse = ModelRetry,
) -> Callable[[str], Awaitable[FetchResult]]:
    """The flat `fetch` tool function: one unit's full text, read by any id form the other tools hand out.

    A collection-qualified `page:`, `section:` or `unit:` ref reads the same unit as its bare id.
    A `collection:` or `group:` ref is refused, since a flat read lists nothing. A ref naming another
    collection is refused naming both, and an id that names no unit is refused naming the forms `fetch` takes.

    Args:
        handler: The fetch handler the unit is read through, scoped to `collection`.
        collection: The knowledge base partition every ref resolves within.
        stamp: This knowledge base's own name and freshness, read once after a read succeeds. `None`
            leaves it unset.
        reground: How every refusal tells the agent to find a good id (`reground_phrase`).
        refuse: Builds the error a refusal raises: `ModelRetry` in a toolset, `ToolError` on MCP.

    Returns:
        The `fetch` callable for this one knowledge base.
    """

    async def fetch(unit_id: str) -> FetchResult:
        try:
            resolved = parse_ref(unit_id, collection, reground=reground)
        except BadRefError as exc:
            raise refuse(str(exc)) from None
        if resolved.kind in {"collection", "group"}:
            raise refuse(no_map_refusal(reground))
        if resolved.collection != collection:
            raise refuse(str(WrongCollectionError(collection, resolved.collection, reground=reground)))
        async with read_snapshot():
            unit = await fetch_one(handler, resolved.unit_id)
            if unit is None:
                raise refuse(no_unit_refusal(resolved.unit_id, collection, reground=reground))
            return unit if stamp is None else unit.model_copy(update={"knowledge_base": await stamp()})

    return fetch


def leveled_fetch(
    handlers: RetrievalHandlers[Search],
    envelope: EnvelopeQueries,
    collection: str,
    *,
    stamp: StampSource | None = None,
    knowledge_base: str = "",
    tools: frozenset[str] = TOOL_NAMES,
) -> Callable[[str], Awaitable[FetchEnvelope | FetchUnit]]:
    """The leveled `fetch` tool function: envelope-shaped reads with a legacy full-text fallback.

    Refs from an earlier listing pass through. A bare id from a `search` hit reads as a section when
    it holds `#`, else as a page. No argument opens the knowledge base map. An id outside the leveled
    surface, such as a claim or a raw chunk, is read through the flat `api.fetch`. A ref the grammar
    cannot read, and a well-formed ref that is refused (another knowledge base's `collection:` or
    `group:` ref, or a group value this collection never had), raise `RefRefusal` with their own
    message. Every successful read returns a `FetchEnvelope` or a `FetchUnit` carrying `next_steps`
    and the ref that reads it back.

    Args:
        handlers: The retrieval handlers behind the flat read.
        envelope: The leveled reads for this one collection, under its declared disclosure mode.
        collection: The knowledge base partition every ref resolves within. A ref naming another
            one is refused before any read.
        stamp: This knowledge base's own name and freshness, read once after a read succeeds and
            carried on whichever of the two shapes the call resolves to. `None` leaves it unset.
        knowledge_base: What a caller calls this knowledge base, named in the not-found refusals so
            a reader over several knows which one was asked. `""` leaves them unqualified.
        tools: The read tools the surface registers. Every refusal sends the agent only to those
            (`reground_phrase`). The default is all four.

    Returns:
        The `fetch` callable for this one knowledge base.
    """
    default_ref = f"collection:{collection}"
    named = f" in {knowledge_base!r}" if knowledge_base else ""
    reground = reground_phrase(tools)

    async def flat(unit_id: str) -> FetchUnit:
        """The flat full-text read for an id with no leveled surface, returned as a `FetchUnit`."""
        unit = await legacy_fetch(handlers, unit_id)
        if unit is None:
            raise ModelRetry(no_unit_refusal(unit_id, collection, named=named, reground=reground))
        qualified = unit.model_copy(update={"ref": qualify_ref(f"unit:{unit.id}", collection)})
        return FetchUnit(
            **qualified.model_dump(exclude={"knowledge_base"}),
            next_steps=_flat_fetch_next_steps(qualified),
            knowledge_base=await stamp() if stamp is not None else None,
        )

    async def read(unit_id: str) -> FetchEnvelope | FetchUnit:
        """The leveled read of one ref."""
        try:
            ref = normalize_ref(unit_id, collection, reground=reground)
            resolved = parse_ref(ref, collection, reground=reground)
        except BadRefError as exc:
            raise RefRefusal(str(exc)) from None
        if resolved.collection != collection:
            raise RefRefusal(str(WrongCollectionError(collection, resolved.collection, reground=reground)))
        disclosed: DisclosureEnvelope | None = None
        if resolved.kind != "unit":
            try:
                disclosed = await envelope(ref)
            except RefRefusedError as exc:
                raise RefRefusal(str(exc)) from None
            except BadRefError:
                disclosed = None
        if disclosed is None:
            return await flat(resolved.unit_id)
        return FetchEnvelope(
            **disclosed.model_dump(exclude={"knowledge_base"}),
            next_steps=envelope_next_steps(disclosed, collection),
            knowledge_base=await stamp() if stamp is not None else None,
        )

    return _in_one_snapshot(read, default_ref)


def _in_one_snapshot(read: Callable[[str], Awaitable[FetchEnvelope | FetchUnit]], default_ref: str) -> Callable[[str], Awaitable[FetchEnvelope | FetchUnit]]:
    """The `fetch` tool over `read`, every read of one call in one snapshot."""

    async def fetch(unit_id: str = default_ref) -> FetchEnvelope | FetchUnit:
        async with read_snapshot():
            return await read(unit_id)

    return fetch


def fetch_opens_map(envelope: EnvelopeHandler | None, tools: frozenset[str]) -> bool:
    """Whether the `fetch` a single-source surface registers opens a map, so its `search` may offer one.

    Args:
        envelope: The envelope handler the surface's `fetch` reads through, or `None` for the flat read.
        tools: The read tools the surface registers.

    Returns:
        True when `fetch` is registered, reads leveled, and its knowledge base is not `search_only`.
    """
    return envelope is not None and FETCH in tools and envelope.mode != "search_only"


def build_retrieval_toolset(
    handlers: RetrievalHandlers[Search],
    *,
    only: frozenset[str] | None = None,
    search_hybrid: bool | None = None,
    switches: RetrievalSwitches | None = None,
    descriptions: Mapping[str, str] | None = None,
    collection: str,
    envelope: EnvelopeHandler | None = None,
    k_source: Callable[[], int] | None = None,
    stamp: StampSource | None = None,
    can_sql: bool = False,
    pinned: bool = False,
    facet_keys: tuple[str, ...] = (),
    hits_per_document: int = 0,
) -> RetrievalToolset:
    """Build a pydantic-ai `FunctionToolset` of engram's retrieval tools, or a subset of them.

    Args:
        handlers: The retrieval handlers the tools wrap.
        only: When given, register only the named tools, a subset of `{"search", "fetch",
            "graph_expand", "find_paths"}`. `None` (the default) registers all four.
        search_hybrid: Fixes the `search` tool's leg and drops its `bm25_only` argument (`True`
            hybrid, `False` keyword). `None` (the default) keeps `bm25_only` for the agent. An
            explicit value always wins over `switches`.
        switches: The knowledge base's resolved retrieval switches, for an auto-mode knowledge base
            at build time. `None` (the default) for a fixed-mode knowledge base. When given and
            `search_hybrid` is `None`, an off embeddings switch forces keyword-only. An off graph
            switch withholds `graph_expand` and `find_paths`, even when `only` names them.
        descriptions: A per-tool tuning override keyed by canonical tool name. A named tool
            gets the override in place of its default. `None` keeps every default.
        collection: The knowledge base partition the `search` tool scopes to, and the map the leveled
            `fetch` opens: the knowledge base's own `retrieval.collection`. Required, since there is no
            default collection. `search` refuses any other `collection` it is passed, naming this one.
        envelope: When given, `fetch` becomes the leveled read (`leveled_fetch`) over this
            handler. `None` keeps the flat full-text fetch (`flat_fetch`), and `search` then offers no map.
        k_source: The row cap when the host owns it rather than the agent, read on every call.
            When set, `search` takes no `k` and its description drops every sentence about it.
            `None` keeps `k` an agent-argued parameter with its own default.
        stamp: Names the knowledge base that answered and how fresh its indexed copy is, read on
            every call. When given, it rides every `search` and `fetch` answer as `knowledge_base`.
            `None` (the default) leaves `knowledge_base` unset.
        can_sql: Whether the caller binds `run_sql` onto this same toolset. `False` (the default)
            keeps the `search` text from sending the agent to a SQL read. `True` keeps that sentence.
        pinned: Whether every `search` reads `collection` alone. `True` drops `collection` from the
            tool's parameters and its sentences from the description, and a collection the model passes
            anyway is ignored. `False` (the default) keeps it, and `search` refuses a value other than
            `collection`. `since`, `until` and `facets` stay either way.
        facet_keys: The facet keys the knowledge base's schema declares. `search` takes `facets` over
            these keys only and refuses any other key with a `ModelRetry` naming them. Its shipped
            description names them, and a `descriptions` override replaces that sentence with the rest.
            Empty (the default) refuses every key, saying the knowledge base declares no facets.
        hits_per_document: The most hits one document keeps, as the knowledge base's search tuning sets it, which the shipped
            `search` description states. 0 (the default) keeps every hit and states nothing about documents.

    Returns:
        A `FunctionToolset` exposing the selected subset of `search` / `fetch` / `graph_expand` /
        `find_paths`.
    """
    resolved_hybrid = _resolve_search_hybrid(search_hybrid, switches)
    selected = _switch_gated_selection(TOOL_NAMES if only is None else only, switches)
    leveled = envelope is not None and FETCH in selected
    capped = k_source if SEARCH in selected else None
    search_type = _scoped_search_type(collection)
    final = _final_descriptions(
        search_hybrid=resolved_hybrid,
        descriptions=descriptions,
        k_source=capped,
        selected=selected,
        can_sql=can_sql,
        pinned=pinned,
        facets_clause=_facets_clause(facet_keys),
        collection=collection,
        served=collection,
        hits_per_document=hits_per_document,
    )
    # `SEARCH` and `FETCH` are always withheld from the shared builder. engram registers them below, so
    # every search result carries `next_steps` and `fetch` reads every id form the other tools hand out.
    toolset = _build_retrieval_toolset(
        handlers,
        available=selected,
        descriptions=final,
        only=selected - {SEARCH, FETCH},
        search_hybrid=resolved_hybrid,
        search_type=search_type,
        stamp=stamp,
    )
    if SEARCH in selected:
        _add_search_tool(
            toolset,
            handlers,
            k_source=k_source,
            search_hybrid=resolved_hybrid,
            description=final[SEARCH],
            search_type=search_type,
            default_collection=collection,
            stamp=stamp,
            pinned=pinned,
            facet_keys=facet_keys,
            browsable=fetch_opens_map(envelope, selected),
            can_fetch=FETCH in selected,
        )
    if leveled and envelope is not None:
        toolset.add_function(
            leveled_fetch(handlers, envelope, collection, stamp=stamp, tools=selected),
            takes_ctx=False,
            name=FETCH,
            description=leveled_fetch_description(collection, mode=envelope.mode, can_search=SEARCH in selected),
            retries=TOOL_MAX_RETRIES,
        )
    elif FETCH in selected:
        toolset.add_function(
            flat_fetch(handlers.fetch, collection, stamp=stamp, reground=reground_phrase(selected)),
            takes_ctx=False,
            name=FETCH,
            description=final[FETCH],
            retries=TOOL_MAX_RETRIES,
        )
    return toolset


def register_mcp_tools(
    server: FastMCP,
    handlers: RetrievalHandlers[Search],
    *,
    only: frozenset[str] | None = None,
    search_hybrid: bool | None = None,
    switches: RetrievalSwitches | None = None,
    descriptions: Mapping[str, str] | None = None,
    collection: str,
    envelope: EnvelopeHandler | None = None,
    k_source: Callable[[], int] | None = None,
    stamp: StampSource | None = None,
    facet_keys: tuple[str, ...] = (),
    hits_per_document: int = 0,
) -> None:
    """Register engram's four retrieval tools on a FastMCP `server`.

    The tools and descriptions match what `build_retrieval_toolset` builds.

    Args:
        server: The `FastMCP` instance to register the tools on.
        handlers: The retrieval handlers the tools wrap.
        only: When given, register only the named tools, a subset of `{"search", "fetch",
            "graph_expand", "find_paths"}`. `None` (the default) registers all four.
        search_hybrid: Fixes the search leg (`True` hybrid, `False` keyword, `None` for the agent),
            as in `build_retrieval_toolset`. An explicit value always wins over `switches`.
        switches: The knowledge base's resolved retrieval switches, for an auto-mode knowledge base
            at build time. `None` (the default) for a fixed-mode knowledge base. They apply as in
            `build_retrieval_toolset`.
        descriptions: A per-tool tuning override keyed by canonical tool name. `None` keeps
            every default.
        collection: The knowledge base partition the `search` tool scopes to, and the map the leveled
            `fetch` opens: the knowledge base's own `retrieval.collection`. Required, since there is no
            default collection. `search` refuses any other `collection` it is passed, naming this one.
        envelope: When given, `fetch` becomes the leveled read (`leveled_fetch`) over this
            handler. `None` keeps the flat full-text fetch (`flat_fetch`), and `search` then offers no map.
        k_source: The row cap when the host owns it rather than the agent, read on every call.
            When set, `search` takes no `k` and its description drops every sentence about it.
            `None` keeps `k` an agent-argued parameter.
        stamp: Names the knowledge base that answered and how fresh its indexed copy is, read on
            every call. When given, it rides every `search` and `fetch` answer as `knowledge_base`.
            `None` (the default) leaves `knowledge_base` unset.
        facet_keys: The facet keys the knowledge base's schema declares, which `search` takes in
            `facets` as in `build_retrieval_toolset`. Any other key fails the call with a tool error
            naming them. Empty (the default) refuses every key.
        hits_per_document: The most hits one document keeps, as the knowledge base's search tuning sets it, which the shipped
            `search` description states. 0 (the default) keeps every hit and states nothing about documents.
    """
    resolved_hybrid = _resolve_search_hybrid(search_hybrid, switches)
    selected = _switch_gated_selection(TOOL_NAMES if only is None else only, switches)
    leveled = envelope is not None and FETCH in selected
    capped = k_source if SEARCH in selected else None
    search_type = _scoped_search_type(collection)
    final = _final_descriptions(
        search_hybrid=resolved_hybrid,
        descriptions=descriptions,
        k_source=capped,
        selected=selected,
        facets_clause=_facets_clause(facet_keys),
        collection=collection,
        served=collection,
        hits_per_document=hits_per_document,
    )
    # `SEARCH` and `FETCH` are always withheld from the shared builder, as in `build_retrieval_toolset`.
    _register_mcp_tools(
        server,
        handlers,
        available=selected,
        descriptions=final,
        only=selected - {SEARCH, FETCH},
        search_hybrid=resolved_hybrid,
        search_type=search_type,
        stamp=stamp,
    )
    if SEARCH in selected:
        _add_search_mcp_tool(
            server,
            handlers,
            k_source=k_source,
            search_hybrid=resolved_hybrid,
            description=final[SEARCH],
            search_type=search_type,
            default_collection=collection,
            stamp=stamp,
            facet_keys=facet_keys,
            browsable=fetch_opens_map(envelope, selected),
            can_fetch=FETCH in selected,
        )
    if not leveled and FETCH in selected:
        flat = flat_fetch(handlers.fetch, collection, stamp=stamp, reground=reground_phrase(selected), refuse=ToolError)
        server.add_tool(flat, name=FETCH, description=final[FETCH])
    if leveled and envelope is not None:
        inner = leveled_fetch(handlers, envelope, collection, stamp=stamp, tools=selected)

        async def fetch(unit_id: str = f"collection:{collection}") -> FetchEnvelope | FetchUnit:
            try:
                return await inner(unit_id)
            except ModelRetry as exc:
                raise ToolError(str(exc)) from exc

        server.add_tool(fetch, name=FETCH, description=leveled_fetch_description(collection, mode=envelope.mode, can_search=SEARCH in selected))


def register_routed_mcp_tools(
    server: FastMCP,
    surfaces: tuple[ReadSurface, ...],
    *,
    default_knowledge_base: str,
    only: frozenset[str] | None = None,
    descriptions: Mapping[str, str] | None = None,
    k_source: Callable[[], int] | None = None,
    served: frozenset[str] | None = None,
) -> None:
    """Register one `search` and one `fetch` over several registered knowledge bases, routed by collection.

    `search`'s `collection` resolves against `surfaces` alone, and `fetch` reads the collection its
    `unit_id` names. Each surface answers through its own handlers, envelope and freshness stamp.
    Every ref an answer carries reads back verbatim through the same `fetch`.

    A collection no surface serves is a tool error naming the registered ones, raised before any
    handler runs. `search` takes `facets` over the facet keys of the surface it searches
    (`ReadSurface.facet_keys`), and any other key is a tool error naming that surface's keys. Its
    description names every surface's keys.

    Args:
        server: The `FastMCP` instance to register the tools on.
        surfaces: The knowledge bases this server may read, in the order a caller sees them listed.
        default_knowledge_base: The `name` of the surface an omitted `collection` (and an
            unqualified `unit_id`) reads. Must be one of `surfaces`.
        only: When given, register only the named tools, a subset of `{"search", "fetch"}`.
            `None` (the default) registers both. Without `search`, `fetch`'s description and its
            refusals send the agent to the ids the task supplies.
        descriptions: A per-tool tuning override keyed by canonical tool name. A named tool gets the
            override in place of its default. `None` keeps every default.
        k_source: The search row cap when the host owns it rather than the agent, read on every call.
            When set, `search` takes no `k` and its description drops every sentence about it.
            `None` keeps `k` an agent-argued parameter.
        served: Every read tool the server registers, graph tools included, which `fetch`'s refusals
            name. `None` takes it to be `only`, or all four when `only` is `None`.

    Raises:
        ValueError: When two surfaces claim one collection, or when `default_knowledge_base` names none
            of `surfaces`, an empty `surfaces` included.
    """
    refuse_duplicate_collections(surfaces)
    home = _home_surface(surfaces, default_knowledge_base)
    can_search = only is None or SEARCH in only
    searching, reading = _routed_descriptions(
        surfaces, home, descriptions=descriptions, k_source=k_source, can_search=can_search, can_fetch=only is None or FETCH in only
    )
    if can_search:
        server.add_tool(
            _routed_search(surfaces, home, k_source=k_source, refuse=ToolError, can_fetch=only is None or FETCH in only),
            name=SEARCH,
            description=searching,
        )
    if only is None or FETCH in only:
        server.add_tool(
            _routed_fetch(surfaces, home, refuse=ToolError, tools=served if served is not None else (TOOL_NAMES if only is None else only)), name=FETCH, description=reading
        )


def add_routed_read_tools(
    toolset: RetrievalToolset,
    surfaces: tuple[ReadSurface, ...],
    *,
    default_knowledge_base: str,
    descriptions: Mapping[str, str] | None = None,
    k_source: Callable[[], int] | None = None,
) -> None:
    """Add the routed `search` and `fetch` to `toolset`, as `register_routed_mcp_tools` does over MCP.

    Routing, descriptions and the facet keys `search` takes are the same. A refusal raises `ModelRetry`
    here rather than a tool error.

    Args:
        toolset: The toolset to register both tools on.
        surfaces: The knowledge bases this toolset may read, in the order a caller sees them listed.
        default_knowledge_base: The `name` of the surface an omitted `collection` (and an unqualified
            `unit_id`) reads. Must be one of `surfaces`.
        descriptions: A per-tool tuning override keyed by canonical tool name. `None` keeps every default.
        k_source: The search row cap when the host owns it rather than the agent, read on every call.
            When set, `search` takes no `k`. `None` keeps `k` an agent-argued parameter.

    Raises:
        ValueError: When two surfaces claim one collection, or when `default_knowledge_base` names
            none of `surfaces`.
    """
    refuse_duplicate_collections(surfaces)
    home = _home_surface(surfaces, default_knowledge_base)
    searching, reading = _routed_descriptions(surfaces, home, descriptions=descriptions, k_source=k_source)
    toolset.add_function(
        _routed_search(surfaces, home, k_source=k_source, refuse=ModelRetry),
        takes_ctx=False,
        name=SEARCH,
        description=searching,
        retries=TOOL_MAX_RETRIES,
    )
    toolset.add_function(
        _routed_fetch(surfaces, home, refuse=ModelRetry), takes_ctx=False, name=FETCH, description=reading, retries=TOOL_MAX_RETRIES
    )


def _routed_descriptions(
    surfaces: tuple[ReadSurface, ...],
    home: ReadSurface,
    *,
    descriptions: Mapping[str, str] | None,
    k_source: Callable[[], int] | None,
    can_search: bool = True,
    can_fetch: bool = True,
) -> tuple[str, str]:
    """The `search` and `fetch` descriptions a routed registration serves, in that order."""
    choices = ", ".join(surface.collection for surface in surfaces)
    facets_clause = declared_facets_clause({surface.collection: surface.facet_keys for surface in surfaces}, several_per_call=False)
    resolved = _final_descriptions(
        search_hybrid=home.hybrid,
        descriptions=descriptions,
        k_source=k_source,
        selected=frozenset({SEARCH, FETCH}) if can_fetch else frozenset({SEARCH}),
        facets_clause=facets_clause,
        hits_per_document=shared_hits_per_document(surface.hits_per_document for surface in surfaces),
    )[SEARCH]
    reading = resolve_description(
        FETCH, leveled_fetch_description(home.collection, mode=home.envelope.mode, can_search=can_search), descriptions
    )
    return _routed_search_description(resolved, choices, home.collection, can_fetch=can_fetch), f"{reading} {_routed_fetch_clause(choices)}"


def refuse_duplicate_collections(surfaces: tuple[ReadSurface, ...]) -> None:
    """Refuse a reader whose surfaces do not agree on which knowledge base owns a collection.

    A ref names a collection, so each collection may belong to only one surface.

    Args:
        surfaces: The knowledge bases a reader or a server is about to be built over.

    Raises:
        ValueError: When two surfaces declare the same `collection`.
    """
    claimed: set[str] = set()
    for surface in surfaces:
        if surface.collection in claimed:
            claimants = ", ".join(candidate.name for candidate in surfaces if candidate.collection == surface.collection)
            msg = (
                f"more than one knowledge base claims collection {surface.collection!r} ({claimants}): "
                "a ref names a collection, so only the first could ever be read."
            )
            raise ValueError(msg)
        claimed.add(surface.collection)


def _home_surface(surfaces: tuple[ReadSurface, ...], name: str) -> ReadSurface:
    """The surface named `name`, which an omitted `collection` reads. Raises `ValueError` when none carries it."""
    home = next((surface for surface in surfaces if surface.name == name), None)
    if home is None:
        offered = ", ".join(surface.name for surface in surfaces)
        msg = f"default_knowledge_base {name!r} names none of the registered knowledge bases: {offered or 'none supplied'}."
        raise ValueError(msg)
    return home


def _owning(by_collection: Mapping[str, ReadSurface], home: ReadSurface, collection: str | None, refuse: Refuse) -> ReadSurface:
    """The knowledge base serving `collection`, `home` when a call named none, refused through `refuse` when none serves it."""
    if collection is None:
        return home
    surface = by_collection.get(collection)
    if surface is None:
        msg = f"No knowledge base registered here for collection {collection!r}. Registered: {', '.join(by_collection)}."
        raise refuse(msg)
    return surface


def _routed_search(
    surfaces: tuple[ReadSurface, ...], home: ReadSurface, *, k_source: Callable[[], int] | None, refuse: Refuse, can_fetch: bool = True
) -> Callable[..., Awaitable[SearchResult]]:
    """The routed `search`: `collection` names which registered knowledge base answers, by its own facet keys, its next steps naming `fetch` only when served."""
    by_collection = {surface.collection: surface for surface in surfaces}

    async def run(
        query: str, k: int, *, collection: str | None, since: str | None, until: str | None, facets: dict[str, list[str]] | None
    ) -> SearchResult:
        surface = _owning(by_collection, home, collection, refuse)
        return await _run_search(
            surface.handlers,
            query,
            k,
            bm25_only=not await _routed_leg(surface),
            search_type=Search,
            default_collection=surface.collection,
            stamp=surface.stamp,
            since=since,
            until=until,
            facets=facets,
            facet_keys=surface.facet_keys,
            facet_owner=_named_base(surface.collection),
            refuse=refuse,
            browsable=surface.envelope.mode != "search_only",
            can_fetch=can_fetch,
        )

    if k_source is None:

        async def search(
            query: str,
            k: int = 12,
            collection: str | None = None,
            *,
            since: Period | None = None,
            until: Period | None = None,
            facets: dict[str, list[str]] | None = None,
        ) -> SearchResult:
            return await run(query, k, collection=collection, since=since, until=until, facets=facets)

        return search

    resolve_k = k_source

    async def search_capped(
        query: str,
        collection: str | None = None,
        *,
        since: Period | None = None,
        until: Period | None = None,
        facets: dict[str, list[str]] | None = None,
    ) -> SearchResult:
        return await run(query, resolve_k(), collection=collection, since=since, until=until, facets=facets)

    return search_capped


def _routed_fetch(
    surfaces: tuple[ReadSurface, ...], home: ReadSurface, *, refuse: Refuse, tools: frozenset[str] = TOOL_NAMES
) -> Callable[[str], Awaitable[FetchEnvelope | FetchUnit]]:
    """The routed `fetch`: the collection a ref names picks the knowledge base that reads it, refusals naming only `tools`."""
    by_collection = {surface.collection: surface for surface in surfaces}
    reground = reground_phrase(tools)
    reads = {
        surface.collection: leveled_fetch(
            surface.handlers, surface.envelope, surface.collection, stamp=surface.stamp, knowledge_base=surface.collection, tools=tools
        )
        for surface in surfaces
    }
    default_ref = f"collection:{home.collection}"

    async def fetch(unit_id: str = default_ref) -> FetchEnvelope | FetchUnit:
        ref = unit_id or default_ref
        try:
            owner = _owning(by_collection, home, parse_ref(ref, home.collection, reground=reground).collection, refuse)
            return await reads[owner.collection](ref)
        except BadRefError as exc:
            raise refuse(str(exc)) from None
        except ModelRetry as exc:
            raise refuse(str(exc)) from None

    return fetch


def _routed_search_description(resolved: str, choices: str, default_collection: str, *, can_fetch: bool = True) -> str:
    """`resolved` pointed at the registered knowledge bases and, where `fetch` is served, at the `ref` that reads a hit back."""
    reads_back = " so pass a hit's `ref` to `fetch` exactly as returned." if can_fetch else "."
    routed = (
        f"`collection` names which knowledge base to search (one of: {choices}) and omitting it searches "
        f"'{default_collection}'. No other collection is served here. Every hit's `ref` already names the "
        f"knowledge base it came from{',' if can_fetch else ''}{reads_back}"
    )
    if _COLLECTION_CLAUSE not in resolved:
        return f"{resolved}\n\n{routed}"
    return resolved.replace(_COLLECTION_CLAUSE, routed).replace(
        " Start here to find candidate sources, then `fetch` a hit's full text by `id`.", " Start here to find candidate sources."
    )


def _routed_fetch_clause(choices: str) -> str:
    """What `fetch`'s description says about reading a ref that belongs to another knowledge base."""
    return (
        f"A `ref` from a hit or a listing already names its knowledge base ({choices}), so pass it back exactly "
        "as returned and it opens in that one. An id with no knowledge base in it reads this server's own."
    )


async def _routed_leg(surface: ReadSurface) -> bool:
    """Whether `surface`'s search runs its vector leg on this call, under its own stored switches."""
    if not surface.hybrid or surface.switch_source is None:
        return surface.hybrid
    return (await surface.switch_source()).embeddings
