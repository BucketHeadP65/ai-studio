"""The infrastructure section every application shares: one model per external system it connects to.

Each model holds where the system is and the credential it takes, nothing more. A model choice, such as
which deployment embeds or summarises, belongs to the section that uses it. Real values live only in the
gitignored settings file, never in the committed example.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    from pathlib import Path


class DatabaseSettings(BaseModel):
    """The Postgres database: the owner URL for migrations and indexing, and the read-only URL for retrieval.

    Both are required. The committed example names the local database, and the settings file or an
    environment variable names any other.
    """

    url: str
    """The owner asyncpg URL that alembic and the indexer connect with."""
    reader_url: str
    """The read-only `kb_reader` asyncpg URL that the serving layer connects with."""
    reader_secret: str = ""
    """The secret each private knowledge base's reader password is derived from.

    The indexer sets that password and the reader derives it to connect, so both must read the same secret. Empty,
    a private knowledge base reads as the anonymous reader, which sees none of it.
    """


class AzureOpenAiSettings(BaseModel):
    """An Azure OpenAI resource, reached through Managed Identity.

    Every deployment on it, for embeddings or chat, goes through this one endpoint, and each consumer
    names its own deployment. An empty endpoint builds, and a path that calls the resource refuses it at
    use time.
    """

    endpoint: str = ""


class DocumentIntelligenceSettings(BaseModel):
    """An Azure Document Intelligence resource for converting documents to markdown, reached through Managed Identity.

    An empty endpoint builds, and the conversion path refuses it at use time.
    """

    endpoint: str = ""


class LogfireSettings(BaseModel):
    """Pydantic Logfire: the regional base URL, the token that writes spans and the token that may only read.

    Empty tokens build, and the paths that ship or query spans refuse them at use time.
    """

    write_token: str = ""
    read_token: str = ""
    base_url: str = "https://logfire-eu.pydantic.dev"


class GatewaySettings(BaseModel):
    """The Pydantic AI Gateway that model calls go through: its API key and its regional proxy URL.

    An empty key builds, and whoever routes a model through the gateway refuses it at use time.
    """

    api_key: str = ""
    base_url: str = "https://gateway-eu.pydantic.dev/proxy"
    typesafe_route: str = "type-safe"
    """The gateway route that reaches TypeSafe, which serves the decision models."""


class OpenRouterSettings(BaseModel):
    """OpenRouter, for the chat models served only there. An empty key means none is on file."""

    api_key: str = ""


class HarnessServiceSettings(BaseModel):
    """Where the harness is reached: for a run's fixed values, and to report ended refresh runs and each index's changed pages.

    A client never supplies this address. The default is the loopback port the harness serves on.
    """

    base_url: str = "http://127.0.0.1:8770"
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    """How long one request may take before it is refused."""


class InfrastructureSettings(BaseModel):
    """Every external system an application connects to.

    `database` is required, and every other system has a default that builds.
    """

    database: DatabaseSettings
    azure_openai: AzureOpenAiSettings = Field(default_factory=AzureOpenAiSettings)
    document_intelligence: DocumentIntelligenceSettings = Field(default_factory=DocumentIntelligenceSettings)
    logfire: LogfireSettings = Field(default_factory=LogfireSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)
    harness: HarnessServiceSettings = Field(default_factory=HarnessServiceSettings)
    embeddings: Literal["azure_openai", "gateway"] = "azure_openai"
    """Which connection serves embeddings: Azure OpenAI with an Entra ID token, or the Pydantic AI Gateway with its key."""


class _SettingsFile(BaseModel):
    """The one section of a settings file this module reads."""

    infrastructure: InfrastructureSettings


def read_infrastructure(path: Path) -> InfrastructureSettings:
    """The `infrastructure` section of the settings file at `path`.

    Args:
        path: The settings file.

    Returns:
        Its infrastructure section.

    Raises:
        OSError: There is no readable file at `path`.
        ValueError: The file is not JSON, or its `infrastructure` section is missing or invalid.
    """
    text = path.read_text(encoding="utf-8")
    try:
        return _SettingsFile.model_validate_json(text).infrastructure
    except ValidationError as exc:
        msg = f"{path} has no valid `infrastructure` section: {exc}"
        raise ValueError(msg) from exc
