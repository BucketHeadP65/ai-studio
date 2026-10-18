"""The errors the refresh area raises."""

from __future__ import annotations


class RefreshForwardFailedError(RuntimeError):
    """The harness did not take an ended refresh run, so the worker delivers it again on its next pass."""
