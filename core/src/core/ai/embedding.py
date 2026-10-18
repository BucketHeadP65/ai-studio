"""Batch text-embedding port, the query embedder port, and a pydantic-ai adapter.

`BatchEmbedder` embeds a batch of texts into vectors, reports the input tokens the provider counted, and names the
model identity and dimensionality to record. `PydanticAiEmbedder` groups the texts into requests under a text cap and
an optional token cap, keeps a bounded number of requests in flight, and hands the vectors back in input order.
A `QueryEmbedder` that also names its model is a `ModelQueryEmbedder`, and `query_model_of` reads that name.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic_ai.embeddings import EmbeddingSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pydantic_ai.embeddings import EmbeddingResult


class QueryEmbedder(Protocol):
    """Places one query in the vector space a knowledge base was embedded in.

    The vector ranks correctly only against vectors from the same model at the same dimensionality.
    Whoever composes the embedder owns that guarantee.
    """

    async def __call__(self, text: str, /) -> Sequence[float]:
        """The query's vector, as the knowledge base was embedded.

        Args:
            text: The query text.

        Returns:
            The vector.
        """
        ...


@runtime_checkable
class ModelQueryEmbedder(QueryEmbedder, Protocol):
    """A query embedder that names the model its vectors come from, so a search compares them only with that model's."""

    @property
    def model_id(self) -> str:
        """The embedding model identity, as the indexer records it. Empty when it is not known."""
        ...


def query_model_of(embed: QueryEmbedder | None) -> str | None:
    """The model a query embedder names, or None when it names none.

    Args:
        embed: The query embedder, or None.

    Returns:
        Its non-empty `model_id`, or None.
    """
    if isinstance(embed, ModelQueryEmbedder):
        return embed.model_id or None
    return None


@dataclass(frozen=True)
class BatchQueryEmbedder:
    """A `ModelQueryEmbedder` over an `BatchEmbedder`: one query is a batch of one, named by the port's model."""

    port: BatchEmbedder
    """The embedder whose model the indexer recorded."""

    @property
    def model_id(self) -> str:
        """The port's model identity."""
        return self.port.model_id

    async def __call__(self, text: str, /) -> Sequence[float]:
        """The query's vector.

        Args:
            text: The query text.

        Returns:
            The vector.
        """
        return (await self.port.embed_batch([text])).vectors[0]


@dataclass(frozen=True)
class Embeddings:
    """The vectors for one batch of texts and the input tokens the provider counted for them."""

    vectors: list[list[float]]
    """One vector per input text, in input order."""
    input_tokens: int
    """The provider's input token count, summed over every request the batch took."""


@runtime_checkable
class BatchEmbedder(Protocol):
    """Embeds batches of text, plus the model identity and dimensionality to record.

    `embed_batch` returns one vector per input text, in order, with the input tokens they cost.
    """

    @property
    def model_id(self) -> str:
        """The embedding model identity."""
        ...

    @property
    def dimensions(self) -> int:
        """The embedding vector dimensionality."""
        ...

    async def embed_batch(self, texts: Sequence[str]) -> Embeddings:
        """Embed `texts`, one vector per input in the same order.

        Args:
            texts: The texts to embed.

        Returns:
            The vectors in input order and the input tokens they cost.
        """
        ...


class SupportsEmbedDocuments(Protocol):
    """The single `Embedder` capability the adapter needs."""

    async def embed_documents(self, documents: Sequence[str], *, settings: EmbeddingSettings | None = ...) -> EmbeddingResult:
        """Embed a batch of documents, one vector per input in order.

        Args:
            documents: The texts to embed.
            settings: The embedding settings, or the embedder's defaults.

        Returns:
            The embedding result, one vector per document.
        """
        ...


def _no_truncation(text: str) -> str:
    return text


def split_into_requests(sizes: Sequence[int], *, batch_size: int, batch_tokens: int | None) -> list[range]:
    """Split texts, given each one's token count, into consecutive requests in input order.

    A request closes when the next text would take it past `batch_size` texts or past `batch_tokens`
    tokens. A text larger than `batch_tokens` on its own goes in a request by itself. `None` caps
    nothing by tokens.

    Args:
        sizes: Each text's token count, in input order.
        batch_size: The most texts one request holds.
        batch_tokens: The most tokens one request holds, or `None` for no token cap.

    Returns:
        One range of input positions per request, covering every position once, in order.
    """
    requests: list[range] = []
    start = 0
    held = 0
    for position, size in enumerate(sizes):
        count = position - start
        over_tokens = batch_tokens is not None and count > 0 and held + size > batch_tokens
        if count == batch_size or over_tokens:
            requests.append(range(start, position))
            start, held = position, 0
        held += size
    if start < len(sizes):
        requests.append(range(start, len(sizes)))
    return requests


class PydanticAiEmbedder:
    """Grouped, concurrent embedding adapter over a pydantic-ai `Embedder` (an `BatchEmbedder`).

    Texts are grouped into requests in input order under `batch_size` texts and, when `count_tokens` is given,
    `batch_tokens` tokens. At most `concurrency` requests are in flight. One failed request cancels the others
    and its error propagates.
    """

    def __init__(  # noqa: PLR0913 (each keyword is one limit or seam the caller chooses)
        self,
        embedder: SupportsEmbedDocuments,
        *,
        deployment: str,
        dimensions: int,
        batch_size: int,
        batch_tokens: int | None = None,
        concurrency: int = 1,
        count_tokens: Callable[[str], int] | None = None,
        truncate: Callable[[str], str] = _no_truncation,
    ) -> None:
        """Wrap `embedder`.

        Args:
            embedder: The pydantic-ai embedder to call.
            deployment: The deployment name, reported as the model identity.
            dimensions: The vector dimensionality to request and report.
            batch_size: The most texts one request holds.
            batch_tokens: The most tokens one request holds. It applies only with `count_tokens`.
            concurrency: The most requests in flight at once.
            count_tokens: Counts one text's tokens, after `truncate`. `None` groups by `batch_size` alone.
            truncate: Applied to each text before it is counted and sent.

        Raises:
            ValueError: `batch_size`, `batch_tokens` or `concurrency` is below 1.
        """
        for name, value in (("batch_size", batch_size), ("batch_tokens", batch_tokens), ("concurrency", concurrency)):
            if value is not None and value < 1:
                msg = f"{name} must be at least 1, got {value}"
                raise ValueError(msg)
        self._embedder = embedder
        self._deployment = deployment
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._batch_tokens = batch_tokens if count_tokens is not None else None
        self._concurrency = concurrency
        self._count_tokens = count_tokens
        self._truncate = truncate
        self._embed_settings = EmbeddingSettings(dimensions=dimensions)

    @property
    def model_id(self) -> str:
        """The embedding model identity (the deployment name)."""
        return self._deployment

    @property
    def dimensions(self) -> int:
        """The embedding vector dimensionality."""
        return self._dimensions

    async def embed_batch(self, texts: Sequence[str]) -> Embeddings:
        """Embed `texts`, grouped into requests with a bounded number in flight, vectors in input order.

        Each text passes through `truncate` before it is counted and sent.

        Args:
            texts: The texts to embed.

        Returns:
            The vectors in input order and the input tokens the provider counted.

        Raises:
            Exception: the first request's error, after the requests still in flight are cancelled.
        """
        items = [self._truncate(text) for text in texts]
        if not items:
            return Embeddings(vectors=[], input_tokens=0)
        count = self._count_tokens
        sizes = [count(item) for item in items] if count is not None else [0] * len(items)
        requests = split_into_requests(sizes, batch_size=self._batch_size, batch_tokens=self._batch_tokens)
        gate = asyncio.Semaphore(self._concurrency)
        vectors: list[list[float]] = [[] for _ in items]

        async def send(positions: range) -> int:
            async with gate:
                result = await self._embedder.embed_documents([items[position] for position in positions], settings=self._embed_settings)
            for position, embedding in zip(positions, result.embeddings, strict=True):
                vectors[position] = [float(component) for component in embedding]
            return result.usage.input_tokens

        try:
            async with asyncio.TaskGroup() as group:
                sent = [group.create_task(send(positions)) for positions in requests]
        except ExceptionGroup as failures:
            raise failures.exceptions[0] from None  # a caller handles the request's own error, not a group of them
        return Embeddings(vectors=vectors, input_tokens=sum(task.result() for task in sent))
