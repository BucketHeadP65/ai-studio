"""Scoring a dataset with search alone under a search setup: the use case, its values, its ports and the admin source lookup."""

from engram.evals.scoring.domain import (
    DEFAULT_SEARCH_SETUP,
    NDCG_K,
    SCORE_KS,
    SEARCH_SETUPS,
    SOURCE_SCORING_VERSION,
    CaseScore,
    DatasetScore,
    HitRef,
    SearchSetup,
    dataset_score,
    ranked_sources,
    score_case,
    search_setup,
)
from engram.evals.scoring.infrastructure.files import score_report_stem, write_score_report
from engram.evals.scoring.infrastructure.sources import AdminHitSources
from engram.evals.scoring.internal_contracts import HitSources, Searcher
from engram.evals.scoring.service import SEARCH_K, score_dataset

__all__ = [
    "DEFAULT_SEARCH_SETUP",
    "NDCG_K",
    "SCORE_KS",
    "SEARCH_K",
    "SEARCH_SETUPS",
    "SOURCE_SCORING_VERSION",
    "AdminHitSources",
    "CaseScore",
    "DatasetScore",
    "HitRef",
    "HitSources",
    "SearchSetup",
    "Searcher",
    "dataset_score",
    "ranked_sources",
    "score_case",
    "score_dataset",
    "score_report_stem",
    "search_setup",
    "write_score_report",
]
