"""The public surface of the evals shared module.

The retrieval names resolve on first access, so importing this module loads no `engram.retrieval` module.
"""

from typing import TYPE_CHECKING

from core.lazy_imports import lazy_module_attributes

from engram.evals.shared.domain import (
    DEFAULT_DIFFICULTY_THRESHOLDS,
    AnchorHash,
    Archetype,
    DifficultyThresholds,
    ExpectedQa,
    QaCaseMeta,
    QaDifficulty,
    QaInput,
    RatedDifficulty,
    RetrievalAnswer,
    Seed,
    derive_difficulty,
)
from engram.evals.shared.metrics import MRR_KEY, NDCG_PREFIX, PRECISION_PREFIX, RECALL_PREFIX, is_known_metric_key, primary_at_k_key
from engram.evals.shared.read_models import EvaluationCaseResultRow, EvaluationRunSummary

if TYPE_CHECKING:
    from engram.evals.shared.serve_bridge import (
        FIND_PATHS,
        GRAPH_EXPAND,
        SEARCH,
        TOOL_NAMES,
        BadRefError,
        EnvelopeHandler,
        FetchEnvelope,
        FetchHandler,
        GraphExpandHandler,
        GraphPathsHandler,
        RetrievalHandlers,
        RetrievalToolset,
        Search,
        SearchHandler,
        SqlKnowledgeBase,
        build_retrieval_toolset,
        envelope_next_steps,
        graph_next_steps,
        parse_ref,
        paths_next_steps,
        qualify_ref,
        read_hits_per_document,
        search_next_steps,
        search_result_status,
    )

__all__ = [
    "DEFAULT_DIFFICULTY_THRESHOLDS",
    "FIND_PATHS",
    "GRAPH_EXPAND",
    "MRR_KEY",
    "NDCG_PREFIX",
    "PRECISION_PREFIX",
    "RECALL_PREFIX",
    "SEARCH",
    "TOOL_NAMES",
    "AnchorHash",
    "Archetype",
    "BadRefError",
    "DifficultyThresholds",
    "EnvelopeHandler",
    "EvaluationCaseResultRow",
    "EvaluationRunSummary",
    "ExpectedQa",
    "FetchEnvelope",
    "FetchHandler",
    "GraphExpandHandler",
    "GraphPathsHandler",
    "QaCaseMeta",
    "QaDifficulty",
    "QaInput",
    "RatedDifficulty",
    "RetrievalAnswer",
    "RetrievalHandlers",
    "RetrievalToolset",
    "Search",
    "SearchHandler",
    "Seed",
    "SqlKnowledgeBase",
    "build_retrieval_toolset",
    "derive_difficulty",
    "envelope_next_steps",
    "graph_next_steps",
    "is_known_metric_key",
    "parse_ref",
    "paths_next_steps",
    "primary_at_k_key",
    "qualify_ref",
    "read_hits_per_document",
    "search_next_steps",
    "search_result_status",
]

_SERVE_BRIDGE = "engram.evals.shared.serve_bridge"

_LAZY_IMPORTS = {
    "BadRefError": _SERVE_BRIDGE,
    "EnvelopeHandler": _SERVE_BRIDGE,
    "FetchEnvelope": _SERVE_BRIDGE,
    "FetchHandler": _SERVE_BRIDGE,
    "GraphExpandHandler": _SERVE_BRIDGE,
    "GraphPathsHandler": _SERVE_BRIDGE,
    "Search": _SERVE_BRIDGE,
    "SearchHandler": _SERVE_BRIDGE,
    "SqlKnowledgeBase": _SERVE_BRIDGE,
    "RetrievalHandlers": _SERVE_BRIDGE,
    "RetrievalToolset": _SERVE_BRIDGE,
    "build_retrieval_toolset": _SERVE_BRIDGE,
    "envelope_next_steps": _SERVE_BRIDGE,
    "graph_next_steps": _SERVE_BRIDGE,
    "paths_next_steps": _SERVE_BRIDGE,
    "search_next_steps": _SERVE_BRIDGE,
    "search_result_status": _SERVE_BRIDGE,
    "parse_ref": _SERVE_BRIDGE,
    "qualify_ref": _SERVE_BRIDGE,
    "read_hits_per_document": _SERVE_BRIDGE,
    "SEARCH": _SERVE_BRIDGE,
    "GRAPH_EXPAND": _SERVE_BRIDGE,
    "FIND_PATHS": _SERVE_BRIDGE,
    "TOOL_NAMES": _SERVE_BRIDGE,
}


__getattr__, __dir__ = lazy_module_attributes(__name__, _LAZY_IMPORTS, __all__)
