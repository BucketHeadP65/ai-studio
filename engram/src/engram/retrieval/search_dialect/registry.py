"""The dialect registry, enum-keyed dispatch over `SearchDialect` strategies.

A thin `core.ai.registry.Registry` subclass keyed by `DbDialect`. It is built
once by `build_search_dialects` and held as a singleton by the host, so adding a
backend is a one-entry diff in that builder plus a new `SearchDialect` impl, no
edit to the consumers that call `select`.
"""

from __future__ import annotations

from core.ai.registry import Registry

from engram.config import DbDialect

from .dialect import SearchDialect


class SearchDialects(Registry[DbDialect, SearchDialect]):
    """Singleton bag of `SearchDialect` strategies keyed by backend.

    Consumers dispatch via `select` instead of branching on the `DbDialect`
    enum, so a new backend never edits a call site.
    """
