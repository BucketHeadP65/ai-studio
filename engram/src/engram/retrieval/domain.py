r"""The retrieval read-side contract: the generic messages plus engram's facet and collection extensions.

`Search` and `Graph` extend their generic counterparts in `core.ai.retrieval.contracts` with a knowledge base's
facets and collection. Every other message, result model and the `SqlGateway` port are re-exported unchanged.
Result field names such as `page_id`, `hit_level` and `provenance_claim` are a stable contract.
"""

from __future__ import annotations

import calendar
import re

from datetime import date
from typing import Annotated

from core.ai.retrieval.contracts import (
    ColumnInfo,
    DescribeSchema,
    FeedbackCategory,
    FeedbackEntry,
    FeedbackSink,
    Fetch,
    FetchResult,
    FoldedUnit,
    GraphEdge,
    GraphExpand,
    GraphNode,
    GraphPath,
    GraphPaths,
    GraphPathsResult,
    GraphResult,
    LookupValue,
    NextSteps,
    ObjectInfo,
    Row,
    SchemaDescription,
    SearchHit,
    SearchResult,
    SqlGateway,
    SqlResult,
    Status,
    StatusReport,
)
from core.ai.retrieval.contracts import Graph as _Graph
from core.ai.retrieval.contracts import Search as _Search
from pydantic import AfterValidator, Field

__all__ = [
    "ColumnInfo",
    "DescribeSchema",
    "FeedbackCategory",
    "FeedbackEntry",
    "FeedbackSink",
    "Fetch",
    "FetchResult",
    "FoldedUnit",
    "Graph",
    "GraphEdge",
    "GraphExpand",
    "GraphNode",
    "GraphPath",
    "GraphPaths",
    "GraphPathsResult",
    "GraphResult",
    "LookupValue",
    "NextSteps",
    "ObjectInfo",
    "Period",
    "Row",
    "SchemaDescription",
    "Search",
    "SearchHit",
    "SearchResult",
    "SqlGateway",
    "SqlResult",
    "Status",
    "StatusReport",
    "period_end",
    "period_start",
]

_PERIOD = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
_PERIOD_PATTERN = r"^\d{4}(-\d{2}(-\d{2})?)?$"


def _period_parts(text: str) -> tuple[int, int | None, int | None]:
    """The year, month and day `text` names, refusing anything that is not a real year, month or day."""
    refusal = f"{text!r} names no real year, month or day. Write a year (2026), a month (2026-09) or a day (2026-09-14)."
    match = _PERIOD.fullmatch(text)
    if match is None:
        raise ValueError(refusal)
    year, month, day = match.groups()
    parts = (int(year), None if month is None else int(month), None if day is None else int(day))
    # A real calendar date proves the month and the day exist, February 30th included.
    try:
        date(parts[0], parts[1] or 1, parts[2] or 1)
    except ValueError:
        raise ValueError(refusal) from None
    return parts


def period_start(text: str) -> date:
    """The first day of the period `text` names: a year (`2026`), a month (`2026-09`) or a day (`2026-09-14`).

    Args:
        text: The period.

    Returns:
        The first day of the period.

    Raises:
        ValueError: When `text` names no real year, month or day.
    """
    year, month, day = _period_parts(text)
    return date(year, month or 1, day or 1)


def period_end(text: str) -> date:
    """The last day of the period `text` names, so a range ending at `2026-09` keeps all of September.

    Args:
        text: The period, written as for `period_start`.

    Returns:
        The last day of the period.

    Raises:
        ValueError: When `text` names no real year, month or day.
    """
    year, month, day = _period_parts(text)
    if month is None:
        return date(year, 12, 31)
    return date(year, month, day or calendar.monthrange(year, month)[1])


def _checked_period(text: str) -> str:
    """`text` unchanged once it names a real period."""
    period_start(text)
    return text


Period = Annotated[str, Field(pattern=_PERIOD_PATTERN), AfterValidator(_checked_period)]
"""A tool argument naming a year, a month or a day. A value that names none is refused when the arguments are read."""


class Search(_Search):
    """Hybrid search over the api surface (`api.search`), the memory's facet extension of the generic `Search`.

    Adds the memory's facet args on top of the generic `query` / `k` / `hybrid` core: `level` pins
    one retrieval tier, `facets` narrows on any key declared in a page's own `facets` column (e.g.
    `{"<facet>": ["<value>"]}`, where values for one key are OR'd, several keys are AND'd, an absent key
    passes unfiltered), and `collection` scopes the knowledge base partition. `since` and `until` keep only
    pages whose page date falls in that inclusive range, so a page with no page date drops out once either is
    set. A raw chunk passes `facets` and the range only when a current page made from its source passes them.
    `hybrid=False` skips the query embedding and runs the BM25-only RRF leg (a NULL `query_vec`), and `k` is the
    requested row count (the server hard-caps it at 50, the client also rides it as the row cap).
    """

    level: str | None = None
    facets: dict[str, list[str]] = Field(default_factory=dict)
    collection: str | None
    since: date | None = None
    until: date | None = None

    @property
    def is_dated(self) -> bool:
        """Whether a date range narrows this search."""
        return self.since is not None or self.until is not None


class Graph(_Graph):
    """The whole knowledge base graph, the memory's collection-scoping extension of the generic `Graph`.

    Adds `collection` on top of the generic `page_type`/`rel` filters: when set, it overrides
    `GraphHandler`'s own wired collection for that one call, so one handler instance can serve any
    indexed collection on request. `None` (the default) leaves the handler's wired collection in
    force.
    """

    collection: str | None = None
