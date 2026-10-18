"""Retrieval strategies: which retrieval tools a run exposes and in which search mode.

A strategy resolves to a `RetrievalProfile`, the tool names to expose and the forced search leg.
There are two search legs, `hybrid` (vector plus keyword, fused) and `bm25_only` (keyword).
`fetch` always comes with `search`, and `graph_expand` plus `find_paths` form the graph leg.
The `graph-only` strategy also carries `fetch`, so a node the walk reaches can be read in full.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .names import FETCH, FIND_PATHS, GRAPH_EXPAND, RUN_CODE, SEARCH

# `search` plus `fetch` is the search leg, `graph_expand` plus `find_paths` is the graph leg.
# `_GRAPH_ONLY_TOOLS` adds `fetch` to the graph leg, so a reached node can be read.
_SEARCH_TOOLS = frozenset({SEARCH, FETCH})
_GRAPH_TOOLS = frozenset({GRAPH_EXPAND, FIND_PATHS})
_GRAPH_ONLY_TOOLS = _GRAPH_TOOLS | {FETCH}
_ALL_TOOLS = _SEARCH_TOOLS | _GRAPH_TOOLS


class RetrievalStrategy(StrEnum):
    """The named retrieval strategies a run can pick (the closed set a consumer offers).

    * `FULL`, the whole suite: hybrid search plus fetch plus graph (the default, the richest surface).
    * `SEARCH_HYBRID`, vector plus keyword search plus fetch, no graph (the standard hybrid run).
    * `SEARCH_KEYWORD`, keyword-only search plus fetch, no graph (the cheap keyword baseline).
    * `GRAPH_ONLY`, graph traversal plus fetch (`graph_expand`, `find_paths`, `fetch`), no search:
      an id to traverse from must come from the task itself or from a prior traversal, never from a
      search call, but a reached node's full text is still readable.
    * `KEYWORD_GRAPH`, keyword search plus fetch plus graph (keyword retrieval with the graph leg).
    * `CODE_MODE`, the full hybrid suite plus the `run_code` code-execution meta-tool: additionally
      exposes a sandbox a model can drive by writing code, collapsing the retrieval tools into one
      round-trip. Additive, the structured tools stay available too.
    """

    FULL = "full"
    SEARCH_HYBRID = "search-hybrid"
    SEARCH_KEYWORD = "search-keyword"
    GRAPH_ONLY = "graph-only"
    KEYWORD_GRAPH = "keyword-graph"
    CODE_MODE = "code-mode"


@dataclass(frozen=True)
class RetrievalProfile:
    """The primitives a strategy resolves to, what a toolset builder needs.

    `tools` is the tool-name subset to expose. `search_hybrid` forces the `search` leg as a fixed
    variable (`True` hybrid, `False` keyword) or is `None` when the strategy exposes no `search`
    tool (a graph-only run), in which case the search mode is irrelevant.
    """

    tools: frozenset[str]
    search_hybrid: bool | None


# One profile per strategy. `DEFAULT_STRATEGY` is the richest surface, so a run that names no strategy gets every tool.
_STRATEGIES: dict[RetrievalStrategy, RetrievalProfile] = {
    RetrievalStrategy.FULL: RetrievalProfile(tools=_ALL_TOOLS, search_hybrid=True),
    RetrievalStrategy.SEARCH_HYBRID: RetrievalProfile(tools=_SEARCH_TOOLS, search_hybrid=True),
    RetrievalStrategy.SEARCH_KEYWORD: RetrievalProfile(tools=_SEARCH_TOOLS, search_hybrid=False),
    RetrievalStrategy.GRAPH_ONLY: RetrievalProfile(tools=_GRAPH_ONLY_TOOLS, search_hybrid=None),
    RetrievalStrategy.KEYWORD_GRAPH: RetrievalProfile(tools=_ALL_TOOLS, search_hybrid=False),
    # Every tool of `full` plus `run_code`. No other strategy exposes `run_code`.
    RetrievalStrategy.CODE_MODE: RetrievalProfile(tools=_ALL_TOOLS | {RUN_CODE}, search_hybrid=True),
}

DEFAULT_STRATEGY = RetrievalStrategy.FULL

# Every strategy name the registry knows, in a stable order with the default first.
STRATEGY_NAMES: tuple[str, ...] = (
    DEFAULT_STRATEGY.value,
    RetrievalStrategy.SEARCH_HYBRID.value,
    RetrievalStrategy.SEARCH_KEYWORD.value,
    RetrievalStrategy.GRAPH_ONLY.value,
    RetrievalStrategy.KEYWORD_GRAPH.value,
    RetrievalStrategy.CODE_MODE.value,
)

# The strategies a tuning run offers: every name except `code-mode`, which stays valid everywhere else.
EVAL_STRATEGY_NAMES: tuple[str, ...] = tuple(name for name in STRATEGY_NAMES if name != RetrievalStrategy.CODE_MODE.value)


def resolve_strategy(name: str | None) -> RetrievalProfile:
    """Resolve a strategy name to its `RetrievalProfile` (`None` gives the default strategy).

    Args:
        name: A `RetrievalStrategy` value (e.g. `"search-hybrid"`), or `None` for `DEFAULT_STRATEGY`.

    Returns:
        The `RetrievalProfile` a toolset builder consumes.

    Raises:
        ValueError: When `name` is not in `STRATEGY_NAMES`. An unknown name never falls back to the default.
    """
    if name is None:
        return _STRATEGIES[DEFAULT_STRATEGY]
    try:
        strategy = RetrievalStrategy(name)
    except ValueError as exc:
        msg = f"unknown retrieval strategy: {name!r} (expected one of {', '.join(STRATEGY_NAMES)})"
        raise ValueError(msg) from exc
    return _STRATEGIES[strategy]
