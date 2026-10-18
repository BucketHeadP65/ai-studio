"""The link-graph backends: the traversal SQL for a recursive CTE and for Apache AGE.

It holds the `GraphBackendSql` protocol, the `GraphBackends` registry keyed by `GraphBackend`, and the
two backends, `CteGraphBackend` for the portable recursive CTE and `AgeGraphBackend` for Apache AGE.
Import these names from `engram.retrieval.graph_backend`, not from its submodules.
"""

from __future__ import annotations

from engram.config import GraphBackend

from .age import AgeGraphBackend
from .backend import GraphBackendSql
from .cte import CteGraphBackend
from .registry import GraphBackends

__all__ = ["AgeGraphBackend", "CteGraphBackend", "GraphBackend", "GraphBackendSql", "GraphBackends", "build_graph_backends"]


def build_graph_backends() -> GraphBackends:
    """Build the link-graph backend registry, the one place a backend is registered.

    Returns:
        A `GraphBackends` registry wrapping the CTE and AGE backends keyed by
        `GraphBackend`.
    """
    return GraphBackends(by_key={GraphBackend.CTE: CteGraphBackend(), GraphBackend.AGE: AgeGraphBackend()})
