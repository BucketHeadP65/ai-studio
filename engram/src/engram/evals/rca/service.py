"""Run saved root-cause-analysis queries through a Logfire query client and assemble the report."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from engram.evals.rca.domain.rca_report import QueryRow, RcaReport, RcaSection

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from engram.evals.rca.internal_contracts import SupportsLogfireQuery

# Validates the Query API's raw `rows`, so API drift raises a `ValidationError`.
_ROWS_ADAPTER: TypeAdapter[list[QueryRow]] = TypeAdapter(list[QueryRow])


def build_rca_report(client: SupportsLogfireQuery, queries: Mapping[str, str], *, query_names: Iterable[str] | None = None) -> RcaReport:
    """Run the named RCA queries through `client` and assemble a structured `RcaReport`.

    Each name's SQL is looked up in `queries` and run, and its rows are validated. A name absent from
    `queries` becomes an empty section carrying an `error` note.

    Args:
        client: The Logfire query client.
        queries: The loaded `{name: sql}` mapping (from `load_named_queries`).
        query_names: The names to run, in order. Defaults to every name in `queries` (so the
            report covers exactly what was loaded).

    Returns:
        An `RcaReport` with one `RcaSection` per requested name, in request order.

    Raises:
        ValidationError: When the rows a query returns do not match `QueryRow`.
    """
    names = list(query_names) if query_names is not None else list(queries)
    sections = [_run_section(client, name, queries.get(name)) for name in names]
    return RcaReport(sections=sections)


def _run_section(client: SupportsLogfireQuery, name: str, sql: str | None) -> RcaSection:
    """Build one `RcaSection`: run `sql` if present, else record the missing name."""
    if sql is None:
        return RcaSection(name=name, sql=None, rows=[], error=f"query '{name}' is not defined in the saved-SQL file")

    envelope = client.query_json_rows(sql)
    rows = _ROWS_ADAPTER.validate_python(envelope["rows"])
    return RcaSection(name=name, sql=sql, rows=rows)
