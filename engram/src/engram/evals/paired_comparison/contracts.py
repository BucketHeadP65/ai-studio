"""The paired comparison: the results file reader, the comparison and its statistics, the report writer and the errors."""

from engram.evals.paired_comparison.domain import (
    INTERVAL_Z,
    PairedComparison,
    PairedScores,
    QueryTypeScores,
    ResultsFile,
    ResultsHeader,
    ResultsLine,
    SourceLevelRetrieval,
    difference_interval,
    holm_adjusted,
    mcnemar_exact_p,
    paired_comparison_markdown,
    paired_comparisons,
    source_level_retrieval,
)
from engram.evals.paired_comparison.exceptions import ResultsFileUnreadableError, RunsNotComparableError
from engram.evals.paired_comparison.infrastructure.files import read_results, write_paired_comparison

__all__ = [
    "INTERVAL_Z",
    "PairedComparison",
    "PairedScores",
    "QueryTypeScores",
    "ResultsFile",
    "ResultsFileUnreadableError",
    "ResultsHeader",
    "ResultsLine",
    "RunsNotComparableError",
    "SourceLevelRetrieval",
    "difference_interval",
    "holm_adjusted",
    "mcnemar_exact_p",
    "paired_comparison_markdown",
    "paired_comparisons",
    "read_results",
    "source_level_retrieval",
    "write_paired_comparison",
]
