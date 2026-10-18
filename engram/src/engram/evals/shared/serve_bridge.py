"""The retrieval names evals uses, re-exported from `engram.retrieval.contracts` and `core.ai.retrieval.contracts`.

This is the only evals module that imports `engram.retrieval.contracts`.
Other modules reach these names through `engram.evals.shared.contracts`, and retrieval never imports this module.
"""

from __future__ import annotations

from core.ai.retrieval.contracts import FIND_PATHS, GRAPH_EXPAND, SEARCH, TOOL_NAMES, RetrievalHandlers, RetrievalToolset

from engram.retrieval.contracts import (
    BadRefError,
    EnvelopeHandler,
    FetchEnvelope,
    FetchHandler,
    GraphExpandHandler,
    GraphPathsHandler,
    Search,
    SearchHandler,
    SqlKnowledgeBase,
    build_retrieval_toolset,
    envelope_next_steps,
    fetch_opens_map,
    graph_next_steps,
    parse_ref,
    paths_next_steps,
    qualify_ref,
    search_next_steps,
    search_result_status,
)

__all__ = [
    "FIND_PATHS",
    "GRAPH_EXPAND",
    "SEARCH",
    "TOOL_NAMES",
    "BadRefError",
    "EnvelopeHandler",
    "FetchEnvelope",
    "FetchHandler",
    "GraphExpandHandler",
    "GraphPathsHandler",
    "RetrievalHandlers",
    "RetrievalToolset",
    "Search",
    "SearchHandler",
    "SqlKnowledgeBase",
    "build_retrieval_toolset",
    "envelope_next_steps",
    "fetch_opens_map",
    "graph_next_steps",
    "parse_ref",
    "paths_next_steps",
    "qualify_ref",
    "search_next_steps",
    "search_result_status",
]
