"""Azure OpenAI model factories: base-url shaping + `test-model` → TestModel (no network)."""

from __future__ import annotations

import pytest

from openai import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.test import TestModel

from core.ai import create_azure_model, create_azure_openai_client, create_model
from core.ai.models import chat_http_client


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://r.openai.azure.com", "https://r.openai.azure.com/openai/v1/"),
        ("https://r.openai.azure.com/", "https://r.openai.azure.com/openai/v1/"),
        ("https://r.openai.azure.com/openai/v1/", "https://r.openai.azure.com/openai/v1/"),
        ("https://r.openai.azure.com/openai/v1", "https://r.openai.azure.com/openai/v1/"),
    ],
)
def test_client_base_url_is_shaped_to_the_openai_v1_endpoint(endpoint, expected):
    client = create_azure_openai_client(endpoint)
    assert str(client.base_url) == expected


def test_client_construction_needs_no_static_key_or_network():
    # Entra ID auth means no API key is ever baked in: construction must succeed
    # offline (no `az login` / network — the token is resolved lazily per request)
    # and the client must hold no static secret string.
    client = create_azure_openai_client("https://r.openai.azure.com")
    assert not client.api_key  # empty sentinel — auth is the async bearer-token provider, not a baked-in key


def test_test_model_deployment_returns_a_test_model_without_touching_the_network():
    # 'test-model' must short-circuit before any Azure client / credential is built,
    # so unit tests never require `az login` or network access.
    model = create_azure_model("test-model", endpoint="https://unused.example")
    assert isinstance(model, TestModel)


def test_non_test_deployment_builds_a_responses_model():
    model = create_azure_model("gpt-4o", endpoint="https://r.openai.azure.com")
    # Not the test double — a real Azure-backed chat model (construction only; no calls made).
    assert not isinstance(model, TestModel)
    assert model.model_name == "gpt-4o"


def test_create_model_test_spec_returns_test_model():
    assert isinstance(create_model("test-model"), TestModel)


def test_create_model_azure_without_endpoint_raises():
    with pytest.raises(ValueError, match="endpoint"):
        create_model("gpt-4o")


def test_create_model_azure_builds_a_responses_model():
    model = create_model("gpt-4o", endpoint="https://r.openai.azure.com")

    assert not isinstance(model, TestModel)
    assert model.model_name == "gpt-4o"


def test_create_azure_model_builds_a_responses_model_with_its_settings() -> None:
    model = create_azure_model("some-deployment", endpoint="https://example.openai.azure.com", settings={"max_tokens": 7})

    assert isinstance(model, OpenAIResponsesModel)
    assert model.settings is not None
    assert model.settings.get("max_tokens") == 7


def test_an_azure_model_without_a_client_keeps_the_sdks_own_retries() -> None:
    model = create_azure_model("some-deployment", endpoint="https://example.openai.azure.com")

    assert isinstance(model, OpenAIResponsesModel)
    assert model.client.max_retries == DEFAULT_MAX_RETRIES


def test_an_azure_model_on_the_chat_retry_client_leaves_retries_to_it_and_keeps_the_sdks_timeout() -> None:
    http_client = chat_http_client()

    model = create_azure_model("some-deployment", endpoint="https://example.openai.azure.com", http_client=http_client)

    assert isinstance(model, OpenAIResponsesModel)
    assert model.client._client is http_client
    assert model.client.max_retries == 0
    assert model.client.timeout == DEFAULT_TIMEOUT
