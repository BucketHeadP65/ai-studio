"""The embedding factory picks its connection from the settings and refuses a connection it cannot build."""

from __future__ import annotations

import pytest

from core.ai import create_embedding_model
from core.settings import AzureOpenAiSettings, DatabaseSettings, GatewaySettings, InfrastructureSettings

_DATABASE = DatabaseSettings(url="postgresql+asyncpg://o:p@localhost/kb", reader_url="postgresql+asyncpg://r:p@localhost/kb")


def _infrastructure(**fields) -> InfrastructureSettings:
    return InfrastructureSettings(database=_DATABASE, **fields)


def test_azure_openai_is_the_default_connection():
    model = create_embedding_model(
        "text-embedding-3-large", infrastructure=_infrastructure(azure_openai=AzureOpenAiSettings(endpoint="https://r.openai.azure.com"))
    )

    assert model.model_name == "text-embedding-3-large"
    assert model.base_url == "https://r.openai.azure.com/openai/v1/"
    assert model._provider.client.max_retries == 0


def test_gateway_connection_goes_through_the_gateway_openai_route():
    model = create_embedding_model(
        "text-embedding-3-large",
        infrastructure=_infrastructure(
            embeddings="gateway", gateway=GatewaySettings(api_key="pylf_test", base_url="https://gateway-eu.pydantic.dev/proxy")
        ),
    )

    assert model.model_name == "text-embedding-3-large"
    assert model.base_url.startswith("https://gateway-eu.pydantic.dev/proxy/")
    assert model._provider.client.max_retries == 0


def test_azure_openai_without_an_endpoint_names_the_setting():
    with pytest.raises(ValueError, match=r"infrastructure\.azure_openai\.endpoint"):
        create_embedding_model("d", infrastructure=_infrastructure())


def test_gateway_without_a_key_names_the_setting():
    with pytest.raises(ValueError, match=r"infrastructure\.gateway\.api_key"):
        create_embedding_model("d", infrastructure=_infrastructure(embeddings="gateway"))
