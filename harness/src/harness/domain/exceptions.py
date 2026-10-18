"""The errors the inward tier raises."""

from __future__ import annotations


class CardUnreadableError(RuntimeError):
    """The library could not say what one saved workflow's card holds, so nothing was admitted.

    Never the same thing as a workflow that carries no card.
    """
