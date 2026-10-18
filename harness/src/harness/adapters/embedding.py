"""The query-side embedder, built to the contract the knowledge base was embedded under."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai import create_embedding_model
from core.settings import InfrastructureSettings
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import Embedder
from pydantic_ai.embeddings import EmbeddingSettings

from harness.domain.contracts import EmbeddingContract
from harness.settings import settings_file

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from core.ai.embedding import ModelQueryEmbedder


class _EmbeddingIngestion(BaseModel):
    """The embedding fields of the `ingestion` section, with the width required: a guessed width is a different vector space."""

    model_config = ConfigDict(extra="ignore")

    embedding_deployment: str = ""
    embedding_dimensions: int = Field(gt=0)


class _EmbeddingSections(BaseModel):
    """The two sections of the settings file the embedding contract is read from."""

    model_config = ConfigDict(extra="ignore")

    infrastructure: InfrastructureSettings
    ingestion: _EmbeddingIngestion


def embedding_contract(settings_path: Path | None = None) -> EmbeddingContract:
    """What a query must be embedded by to rank against the stored pages, read from the shared settings file.

    The deployment and width are `ingestion.embedding_deployment` and `ingestion.embedding_dimensions`, the same fields indexing
    embeds with. The connection is the one `infrastructure.embeddings` names, which needs `infrastructure.azure_openai.endpoint`
    for `azure_openai` and `infrastructure.gateway.api_key` for `gateway`. Nothing is defaulted. A missing value raises.

    Args:
        settings_path: The shared settings file. `None` reads `settings_file()`.

    Returns:
        The deployment, endpoint and vector width the stored pages were embedded with.

    Raises:
        ValueError: If the file names no deployment, no setting its embedding connection needs, or no usable embedding width.
    """
    path = settings_path if settings_path is not None else settings_file()
    try:
        sections = _EmbeddingSections.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        msg = f"{path} has no valid infrastructure and ingestion sections: {exc}"
        raise ValueError(msg) from exc
    infrastructure = sections.infrastructure
    deployment = sections.ingestion.embedding_deployment
    if infrastructure.embeddings == "gateway":
        connection = ("infrastructure.gateway.api_key", infrastructure.gateway.api_key)
    else:
        connection = ("infrastructure.azure_openai.endpoint", infrastructure.azure_openai.endpoint)
    for name, value in (("ingestion.embedding_deployment", deployment), connection):
        if not value:
            msg = f"{path} names no {name}, so a query cannot be embedded the way the knowledge base was"
            raise ValueError(msg)
    return EmbeddingContract(deployment=deployment, infrastructure=infrastructure, dimensions=sections.ingestion.embedding_dimensions)


@dataclass(frozen=True)
class _DeploymentQueryEmbedder:
    """Embeds a query with one deployment and names that deployment as its model, as the indexer records it."""

    embedder: Embedder
    settings: EmbeddingSettings
    model_id: str

    async def __call__(self, text: str, /) -> Sequence[float]:
        result = await self.embedder.embed_query(text, settings=self.settings)
        return result.embeddings[0]


def query_embedder(contract: EmbeddingContract | None = None) -> ModelQueryEmbedder:
    """An embedder that places a query in the knowledge base's vector space, built with the shared embedding factory.

    It names its deployment as its model, so a search compares its vectors only with that deployment's.

    Args:
        contract: What the query must be embedded by. `None` reads it from the shared settings file.

    Returns:
        An async function from a query's text to its vector, carrying the deployment as `model_id`.

    Raises:
        ValueError: `contract` is `None` and the shared settings file names no deployment or connection setting.
    """
    terms = contract if contract is not None else embedding_contract()
    embedder = Embedder(create_embedding_model(terms.deployment, infrastructure=terms.infrastructure))
    return _DeploymentQueryEmbedder(embedder, EmbeddingSettings(dimensions=terms.dimensions), terms.deployment)
