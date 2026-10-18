"""Loader-parse guard for the committed RCA saved-SQL file.

Hermetic — no network, no Logfire, no DB. This pins the contract between the on-disk
`evals/rca/queries.sql` and the RCA helpers that load it: the file must parse through
:func:`engram.evals.rca.contracts.load_named_queries` and must define the three mandatory RCA
queries (:data:`engram.evals.rca.domain.rca_report._DEFAULT_RCA_QUERY_NAMES`), each mapping to a non-empty
SQL body. It does **not** execute the SQL (that is validated against the live Logfire
project when the file is authored); it guards that the saved-SQL file stays loadable and
keeps the names the report runs by default.
"""

from __future__ import annotations

from pathlib import Path

from engram.config import EvalsSettings
from engram.evals.rca.contracts import load_named_queries
from engram.evals.rca.domain.rca_report import _DEFAULT_RCA_QUERY_NAMES

# The three RCA queries the report runs by default — the latency / recall / token-outlier triad.
_MANDATORY_QUERY_NAMES = ("slowest_tool_spans", "lowest_recall_cases", "token_outliers")


def _queries_path() -> Path:
    """Resolve the committed `evals/rca/queries.sql` robustly.

    Prefers the configured :attr:`~engram.config.EvalsSettings.rca_queries_path` (relative to the
    engram project root, where pytest runs) when it resolves to an existing file; otherwise
    falls back to navigating from this test file (`tests/unit/evals/` -> project root).

    Returns:
        The path to the saved-SQL file.
    """
    configured = EvalsSettings().rca_queries_path
    if configured.exists():
        return configured
    return Path(__file__).resolve().parents[4] / "evals" / "rca" / "queries.sql"


def test_rca_queries_file_exists() -> None:
    assert _queries_path().is_file()


def test_load_named_queries_parses_the_committed_file() -> None:
    queries = load_named_queries(_queries_path())

    # A non-empty mapping of name -> SQL string (the loader's contract).
    assert queries
    assert all(isinstance(name, str) and isinstance(sql, str) for name, sql in queries.items())


def test_committed_file_defines_the_mandatory_rca_queries() -> None:
    queries = load_named_queries(_queries_path())

    # The three names the report runs by default must all be present.
    assert set(_MANDATORY_QUERY_NAMES) <= set(queries)
    # And they must match rca.py's default set exactly (the names the prompt surfaces).
    assert set(_DEFAULT_RCA_QUERY_NAMES) <= set(queries)


def test_each_mandatory_query_maps_to_non_empty_sql() -> None:
    queries = load_named_queries(_queries_path())

    for name in _MANDATORY_QUERY_NAMES:
        assert name in queries
        assert queries[name].strip(), f"query '{name}' has an empty SQL body"


def test_each_mandatory_query_carries_a_time_predicate() -> None:
    # Every saved query must self-window: rca.py runs them single-arg via query_json_rows(sql)
    # and Logfire deprecates a query with no time window. The schema's time column is start_timestamp.
    queries = load_named_queries(_queries_path())

    for name in _MANDATORY_QUERY_NAMES:
        assert "start_timestamp" in queries[name], f"query '{name}' is missing a start_timestamp time predicate"
