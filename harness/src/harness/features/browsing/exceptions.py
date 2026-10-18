"""The errors the browsing slice raises when a workflow's card cannot be rewritten."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.domain.contracts import DraftReadiness


class CardRefusedError(Exception):
    """A card rewrite was refused because a field it carries fails the readiness check.

    The message names what was refused. `readiness` is the reading of the merged card.
    """

    def __init__(self, message: str, *, readiness: DraftReadiness) -> None:
        """Name what was refused and keep the reading it was refused on.

        Args:
            message: What was refused, in words the person can act on.
            readiness: The readiness reading of the merged card.
        """
        super().__init__(message)
        self.readiness = readiness
