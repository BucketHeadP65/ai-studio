"""The errors the indexing area raises."""

from __future__ import annotations


class IndexForwardFailedError(RuntimeError):
    """The harness did not take an index's changed pages, so the worker delivers the event again on its next pass."""
