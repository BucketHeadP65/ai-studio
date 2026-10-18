r"""The Embed stage's `BatchEmbedder`: the shared `PydanticAiEmbedder` with engram's token truncation and counting.

`PydanticAiEmbedder.from_settings` builds it over an embedding model reached through Azure OpenAI or the gateway,
with no environment reads, pinned to the knowledge base's embedding dimensions, with its requests grouped and run under the
ingestion settings' limits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai import create_embedding_model
from core.ai.embedding import PydanticAiEmbedder as _PydanticAiEmbedder
from core.ai.embedding import SupportsEmbedDocuments
from pydantic_ai import Embedder
from pydantic_ai.embeddings import EmbeddingSettings

from engram.indexing.domain.card import token_count, truncate_embed_input

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.settings import InfrastructureSettings

    from engram.config import IngestionSettings

__all__ = ["PydanticAiEmbedder", "SupportsEmbedDocuments"]


class PydanticAiEmbedder(_PydanticAiEmbedder):
    """engram's Azure OpenAI embedder: the shared adapter defaulting to engram's token truncation and token count.

    `embed_batch` truncates each text to the embed-input token budget (`truncate_embed_input`) before
    the network call, so an untruncated text never fails the index on the model's token limit. Requests are
    grouped by `token_count`, the same encoding the truncation uses.
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
        count_tokens: Callable[[str], int] | None = token_count,
        truncate: Callable[[str], str] = truncate_embed_input,
    ) -> None:
        """Wrap `embedder` for one deployment.

        Args:
            embedder: The pydantic-ai embedder that makes the network call.
            deployment: The embedding model deployment name.
            dimensions: The vector dimensions every embedding must have.
            batch_size: The most texts one request holds.
            batch_tokens: The most tokens one request holds.
            concurrency: The most requests in flight at once.
            count_tokens: Counts one text's tokens, after truncation.
            truncate: The function that cuts each text to the token budget before embedding.
        """
        super().__init__(
            embedder,
            deployment=deployment,
            dimensions=dimensions,
            batch_size=batch_size,
            batch_tokens=batch_tokens,
            concurrency=concurrency,
            count_tokens=count_tokens,
            truncate=truncate,
        )

    @classmethod
    def from_settings(cls, infrastructure: InfrastructureSettings, ingestion: IngestionSettings) -> PydanticAiEmbedder:
        """Build the adapter over a real `Embedder`, on the connection `infrastructure.embeddings` names.

        Args:
            infrastructure: The shared infrastructure settings that name the embedding connection.
            ingestion: The embedding deployment, dimensions and request limits.

        Returns:
            The adapter, ready to embed.
        """
        deployment = ingestion.embedding_deployment
        model = create_embedding_model(deployment, infrastructure=infrastructure)
        embedder = Embedder(model, settings=EmbeddingSettings(dimensions=ingestion.embedding_dimensions))
        return cls(
            embedder,
            deployment=deployment,
            dimensions=ingestion.embedding_dimensions,
            batch_size=ingestion.embedding_batch_size,
            batch_tokens=ingestion.embedding_batch_tokens,
            concurrency=ingestion.embedding_concurrency,
        )
