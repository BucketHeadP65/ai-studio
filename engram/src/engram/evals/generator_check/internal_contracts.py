"""What the generator check needs from scoring: one dataset's nDCG@10 under one search setup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class SetupScorer(Protocol):
    """Scores one dataset under one search setup."""

    async def __call__(self, dataset: Path, *, collection: str, setup: str) -> float:
        """The mean nDCG@10 of `dataset` searched under `setup`.

        Args:
            dataset: The dataset file.
            collection: The knowledge base searched.
            setup: The name of the search setup every search runs under.

        Returns:
            The dataset's mean nDCG@10 over its scored cases.
        """
        ...
