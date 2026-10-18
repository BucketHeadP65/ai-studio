"""The `EmbedStrategy` protocol: pure text composition over one indexed unit.

A strategy turns one indexed unit (a resolved note, a source mirror) into zero or more
embeddable units. It does no IO and calls no model, and the same input always yields the
same units.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .unit import EmbedStrategyUnit


class EmbedStrategy[T](Protocol):
    """Given one indexed unit of type `T`, yields the embed units it owns.

    `level` names the granularity tier every unit this strategy yields is stamped with, such as
    `"page"` or `"section"`. A strategy always yields exactly one level.
    """

    level: str

    def units(self, item: T) -> Iterable[EmbedStrategyUnit]:
        """Yield this strategy's embed units for one indexed `item`.

        Args:
            item: The indexed unit to compose text over.

        Returns:
            The embed units, each stamped with `level`, in a stable order.
        """
        ...
