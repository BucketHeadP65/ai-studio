"""The BM25 SQL for each supported database: `pg_search` and `pg_textsearch`.

It holds the `SearchDialect` port, the `SearchDialects` registry keyed by `DbDialect`, and the two
dialects, `PgSearchDialect` and `PgTextSearchDialect`. Import these names from
`engram.retrieval.search_dialect`, never from its sub-modules.
"""

from __future__ import annotations

from engram.config import DbDialect

from .dialect import SearchDialect
from .pg_search import PgSearchDialect
from .pg_textsearch import PgTextSearchDialect
from .registry import SearchDialects

__all__ = ["DbDialect", "PgSearchDialect", "PgTextSearchDialect", "SearchDialect", "SearchDialects", "build_search_dialects"]


def build_search_dialects() -> SearchDialects:
    """Build the BM25 dialect registry, the one place a backend is registered.

    Returns:
        A `SearchDialects` registry wrapping the `pg_search` and `pg_textsearch`
        dialects keyed by `DbDialect`.
    """
    return SearchDialects(by_key={DbDialect.PG_SEARCH: PgSearchDialect(), DbDialect.PG_TEXTSEARCH: PgTextSearchDialect()})
