"""A deterministic in-memory :class:`~core.ai.embedding.BatchEmbedder` with no network.

The same text always yields the same 1024-dimension vector, and different texts yield different
vectors. `model_id` and `dimensions` answer the values the embed stage records in `kb.embed_meta`.

`texts` collects every text embedded across all calls, in order. `batches` keeps the grouping
of each call. `embed_batch` reports the inputs' word count as their input tokens.
"""

from __future__ import annotations

import hashlib
import random

from typing import TYPE_CHECKING

from core.ai.embedding import Embeddings

if TYPE_CHECKING:
    from collections.abc import Sequence

_DIMENSIONS = 1024


def _vector(text: str, dimensions: int) -> list[float]:
    """A deterministic vector for `text`, the same for the same text."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(dimensions)]


class FakeQueryEmbedding:
    """A deterministic in-memory `QueryEmbedder` that records every query text.

    A text embedded here gets the same vector a `FakeEmbedding` gives it.
    """

    def __init__(self, *, dimensions: int = _DIMENSIONS) -> None:
        """Set the vector size.

        Args:
            dimensions: The length of every vector.
        """
        self._dimensions = dimensions
        self.texts: list[str] = []

    async def __call__(self, text: str) -> Sequence[float]:
        """Record the text and answer its vector.

        Args:
            text: The query text.

        Returns:
            The deterministic vector of `text`.
        """
        self.texts.append(text)
        return _vector(text, self._dimensions)


class FakeEmbedding:
    """Records the texts and batches it was asked to embed and answers deterministic vectors."""

    def __init__(self, *, dimensions: int = _DIMENSIONS, model: str = "fake-embed") -> None:
        """Set the vector size and model name.

        Args:
            dimensions: The length of every vector.
            model: The name `model_id` answers.
        """
        self._dimensions = dimensions
        self._model = model
        self.texts: list[str] = []
        self.batches: list[list[str]] = []

    @property
    def model_id(self) -> str:
        """The model name given at construction."""
        return self._model

    @property
    def dimensions(self) -> int:
        """The length of every vector."""
        return self._dimensions

    async def embed_batch(self, texts: Sequence[str]) -> Embeddings:
        """Record one batch and answer a vector per text, with the inputs' word count as their input tokens.

        Args:
            texts: The texts to embed. No text may be empty.

        Returns:
            One vector per text, in order, and the word count of the inputs.
        """
        batch = list(texts)
        # A real embedding service refuses an empty string, so an empty input fails the test.
        assert all(text for text in batch), "FakeEmbedding received an empty embed input"
        self.batches.append(batch)
        self.texts.extend(batch)
        return Embeddings(vectors=[_vector(text, self._dimensions) for text in batch], input_tokens=sum(len(text.split()) for text in batch))
