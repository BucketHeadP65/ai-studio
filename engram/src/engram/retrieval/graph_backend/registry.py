"""The graph-backend registry, enum-keyed dispatch over `GraphBackendSql` strategies.

A thin `core.ai.registry.Registry` subclass keyed by `GraphBackend`. It is built
once by `build_graph_backends` and held as a singleton by the host, so adding a
backend is a one-entry diff in that builder plus a new `GraphBackendSql` impl,
no edit to the consumers that call `select`.
"""

from __future__ import annotations

from core.ai.registry import Registry

from engram.config import GraphBackend

from .backend import GraphBackendSql


class GraphBackends(Registry[GraphBackend, GraphBackendSql]):
    """Singleton bag of `GraphBackendSql` strategies keyed by backend.

    Consumers dispatch via `select` instead of branching on the `GraphBackend`
    enum, so a new backend never edits a call site.
    """
