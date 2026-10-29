"""The model catalog: which gateway route, API, and profile each model id resolves through.

Every model is built with a fake gateway credential handed in, so no test here touches
the settings file or the network. `gateway_provider` builds real HTTP clients with hooks, but never sends a request.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.settings import AzureOpenAiSettings, DatabaseSettings, GatewaySettings, InfrastructureSettings, OpenRouterSettings
from genai_prices import calc_price
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.retries import AsyncHTTPX2TenacityTransport
from pydantic_ai.usage import RequestUsage

from harness.adapters import models
from harness.settings import ModelSettings
from tests.support.price_registry import empty_price_registry

_DATABASE = DatabaseSettings(url="postgresql+asyncpg://owner:pw@db.example:5432/kb", reader_url="postgresql+asyncpg://reader:pw@db.example:5432/kb")
_GATEWAY = InfrastructureSettings(database=_DATABASE, gateway=GatewaySettings(api_key="test-key", base_url="https://gateway.test/proxy"))
_AZURE_ENDPOINT = "https://example.openai.azure.com"
_GATEWAY_AND_OPENROUTER = _GATEWAY.model_copy(
    update={"openrouter": OpenRouterSettings(api_key="or-test-key"), "azure_openai": AzureOpenAiSettings(endpoint=_AZURE_ENDPOINT)}
)
_ROUTES = ModelSettings()
"""The deployment's own route settings, handed to every build here so none reads an export."""


def test_the_default_model_is_glm_flash() -> None:
    assert models.MODEL_ID == "glm-5.3-flash"
    assert models.MODEL_ID in models.CATALOG


def test_the_catalog_lists_the_five_models_default_first() -> None:
    assert tuple(models.CATALOG) == ("glm-5.3-flash", "deepseek-v4-pro-0813", "deepseek-v4-flash-0731", "gpt-5.4", "gpt-6-luna")


def test_an_azure_id_builds_a_responses_model_on_the_endpoints_v1_path_with_the_ceiling() -> None:
    built = models.model_for("gpt-6-luna", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES, thinking="low")

    assert isinstance(built, OpenAIResponsesModel)
    assert built.model_name == "gpt-6-luna"
    assert str(built.client.base_url) == f"{_AZURE_ENDPOINT}/openai/v1/"
    assert built.settings is not None
    assert built.settings.get("max_tokens") == _ROUTES.max_tokens
    assert built.settings.get("thinking") == "low"


def test_an_azure_id_never_routes_through_openrouter_or_the_gateway() -> None:
    built = models.model_for("gpt-6-luna", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES)

    assert isinstance(built, OpenAIResponsesModel)
    assert models.openrouter_wire_name("gpt-6-luna") is None
    assert "openrouter" not in str(built.client.base_url)
    assert "gateway" not in str(built.client.base_url)


def test_an_azure_id_retries_a_dropped_connection_on_its_own_client_and_never_in_the_sdk() -> None:
    built = models.model_for("gpt-6-luna", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES)

    assert isinstance(built, OpenAIResponsesModel)
    assert isinstance(built.client._client._transport, AsyncHTTPX2TenacityTransport)
    assert built.client.max_retries == 0


def test_an_azure_id_needs_the_azure_endpoint() -> None:
    with pytest.raises(ValueError, match=r"infrastructure\.azure_openai\.endpoint"):
        models.model_for("gpt-6-luna", infrastructure=_GATEWAY, models=_ROUTES)


def test_a_zai_id_routes_through_openrouter_under_its_own_prefix() -> None:

    model = models.model_for("glm-5.3-flash", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES)

    assert isinstance(model, OpenRouterModel)
    assert model.model_name == "z-ai/glm-5.3-flash"
    assert model.profile.get("supports_json_object_output") is False


def test_a_zai_id_needs_the_openrouter_credential() -> None:
    with pytest.raises(ValueError, match="reachable through OpenRouter only"):
        models.model_for("glm-5.3-flash", infrastructure=_GATEWAY, models=_ROUTES)


def test_the_openrouter_route_asks_for_the_cost_on_every_response() -> None:

    for model_id in ("glm-5.3-flash", "deepseek-v4-flash-0731"):
        settings = models.model_for(model_id, infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES).settings
        assert settings is not None, model_id
        assert settings.get("openrouter_usage") == {"include": True}, model_id


def test_glm_flash_on_openrouter_is_never_sent_to_together_or_inference_net() -> None:

    settings = models.model_for("glm-5.3-flash", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES).settings

    assert settings is not None
    assert settings.get("openrouter_provider") == {"ignore": ["together", "inference-net"]}


def test_other_openrouter_models_are_never_sent_to_inference_net() -> None:

    for model_id in ("deepseek-v4-flash-0731", "qwen/qwen3-coder"):
        settings = models.model_for(model_id, infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES).settings
        assert settings is not None, model_id
        assert settings.get("openrouter_provider") == {"ignore": ["inference-net"]}, model_id


def test_a_glm_response_is_priced_from_the_registry_on_its_route() -> None:
    usage = RequestUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    response = ModelResponse(
        parts=[], model_name="z-ai/glm-5.3-flash", provider_name="openrouter", usage=usage, timestamp=datetime(2026, 10, 1, tzinfo=UTC)
    )

    assert models.price_response(response) == Decimal("0.65")
    assert models.response_cost(response) == Decimal("0.65")


def test_a_glm_response_the_registry_cannot_price_is_left_unpriced() -> None:
    # A price copied into this app goes stale unwatched, so an unknown price stays unknown.
    usage = RequestUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    response = ModelResponse(parts=[], model_name="z-ai/glm-5.3-flash", provider_name="openrouter", usage=usage)

    with empty_price_registry():
        assert models.price_response(response) is None
        assert models.priced_cost(response) is None


def test_a_catalog_model_the_registry_does_not_carry_has_no_window() -> None:
    with empty_price_registry():
        assert models.context_window_for("glm-5.3-flash") is None


def test_a_glm_response_off_any_known_route_is_left_unpriced() -> None:
    response = ModelResponse(parts=[], model_name="z-ai/glm-5.3-flash", provider_name="somewhere", usage=RequestUsage(input_tokens=10))

    assert models.price_response(response) is None


@pytest.mark.parametrize(
    ("model_id", "window"),
    [
        ("glm-5.3-flash", 1_310_720),
        ("deepseek-v4-pro-0813", 1_000_000),
        ("deepseek-v4-flash-0731", 1_000_000),
        ("gpt-5.4", 1_050_000),
        ("gpt-6-luna", 1_050_000),
        ("gpt-5.4-mini", None),
    ],
)
def test_every_catalog_model_knows_its_window_and_an_outsider_does_not(model_id: str, window: int | None) -> None:
    assert models.context_window_for(model_id) == window


def test_the_figure_the_host_billed_wins_over_every_table() -> None:
    # OpenRouter answers through many hosts and each bills its own rate, so the amount it reports
    # for the response is the only figure that is right for that response.
    usage = RequestUsage(input_tokens=19, output_tokens=32)
    billed = ModelResponse(
        parts=[], model_name="z-ai/glm-5.3-flash", provider_name="openrouter", usage=usage, provider_details={"cost": 1.8848115e-05}
    )
    unbilled = ModelResponse(parts=[], model_name="z-ai/glm-5.3-flash", provider_name="openrouter", usage=usage, provider_details={})

    assert models.billed_cost(billed) == Decimal("0.000018848115")
    assert models.response_cost(billed) == Decimal("0.000018848115")
    assert models.billed_cost(unbilled) is None
    assert models.response_cost(unbilled) == models.price_response(unbilled)


def test_a_deepseek_id_builds_a_chat_model_on_the_deepseek_route() -> None:
    model = models.model_for("deepseek-v4-flash-0731", infrastructure=_GATEWAY, models=_ROUTES)

    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "deepseek-v4-flash-0731"
    assert model.provider is not None
    assert model.provider.base_url == "https://gateway.test/proxy/DeepSeek/"


def test_a_deepseek_model_carries_deepseek_thinking_and_refuses_json_schema() -> None:
    profile = models.model_for("deepseek-v4-pro-0813", infrastructure=_GATEWAY, models=_ROUTES).profile

    assert profile.get("openai_chat_thinking_field") == "reasoning_content"
    assert profile.get("openai_chat_send_back_thinking_parts") == "field"
    assert profile.get("supports_forced_tool_choice") is False
    assert profile.get("supports_json_schema_output") is False
    assert profile.get("supports_json_object_output") is True


def test_a_deepseek_id_routes_through_openrouter_when_the_credential_is_present() -> None:

    model = models.model_for("deepseek-v4-pro-0813", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES)

    assert isinstance(model, OpenRouterModel)
    assert model.model_name == "deepseek/deepseek-v4-pro-0813"
    assert isinstance(model.provider, OpenRouterProvider)
    assert model.provider.base_url == "https://openrouter.ai/api/v1"


def test_the_openrouter_route_covers_both_deepseek_catalog_entries() -> None:

    model = models.model_for("deepseek-v4-flash-0731", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES)

    assert model.model_name == "deepseek/deepseek-v4-flash-0731"
    assert isinstance(model.provider, OpenRouterProvider)


def test_the_openai_catalog_entry_never_routes_through_openrouter() -> None:
    """The credential only redirects DeepSeek; `gpt-5.4` stays on the gateway's own OpenAI route."""
    model = models.model_for("gpt-5.4", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES)

    assert isinstance(model, OpenAIResponsesModel)
    assert model.model_name == "gpt-5.4"
    assert not isinstance(model.provider, OpenRouterProvider)
    assert model.provider is not None
    assert model.provider.base_url == "https://gateway.test/proxy/openai/"


def test_the_gateway_route_stands_unchanged_when_no_openrouter_credential_is_on_file() -> None:
    """`_GATEWAY` carries no `openrouter` key -- the byte-for-byte baseline."""
    model = models.model_for("deepseek-v4-pro-0813", infrastructure=_GATEWAY, models=_ROUTES)

    assert not isinstance(model.provider, OpenRouterProvider)
    assert model.model_name == "deepseek-v4-pro-0813"


def test_openrouter_deepseek_keeps_the_pinned_flags_but_speaks_openrouters_own_thinking_field() -> None:
    """The behavior pins (`_deepseek_profile`) hold on the OpenRouter route; its wire format does not get overwritten."""
    profile = models.model_for("deepseek-v4-pro-0813", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES).profile

    assert profile.get("supports_json_schema_output") is False
    # Off on this route only: OpenRouter's marketplace hosts drop tool calls when a request asks
    # for `json_object` output, so prompted output goes schema-in-prompt with no `response_format`.
    assert profile.get("supports_json_object_output") is False
    assert profile.get("supports_forced_tool_choice") is False
    # OpenRouter's own normalization, left alone: it reshapes DeepSeek's native
    # `reasoning_content` field into `reasoning` before this app ever sees a response.
    assert profile.get("openai_chat_thinking_field") == "reasoning"
    assert profile.get("supports_thinking") is True
    assert profile.get("openai_chat_send_back_thinking_parts") == "field"


def test_the_flash_tier_keeps_tool_choice_required_pinned_off_through_openrouter_too() -> None:

    profile = models.model_for("deepseek-v4-flash-0731", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES).profile

    assert profile.get("supports_forced_tool_choice") is False


def _ceiling_of(model_id: str) -> int | None:
    """The response ceiling a built model carries, or None when it carries no settings at all."""
    settings = models.model_for(model_id, infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES).settings
    return None if settings is None else settings.get("max_tokens")


@pytest.mark.parametrize("model_id", ["glm-5.3-flash", "deepseek-v4-pro-0813", "gpt-5.4", "gpt-6-luna"])
def test_every_route_asks_for_the_thinking_level_it_was_given(model_id: str) -> None:

    built = models.model_for(model_id, thinking="low", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES)

    assert built.settings is not None
    assert built.settings.get("thinking") == "low"


def test_the_thinking_level_defaults_to_the_top_of_the_scale() -> None:

    built = models.model_for("glm-5.3-flash", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES)

    assert built.settings is not None
    assert built.settings.get("thinking") == models.DEFAULT_THINKING


def test_an_id_outside_the_catalog_builds_on_openrouter() -> None:

    built = models.model_for("openai/gpt-5.4-mini", thinking="medium", infrastructure=_GATEWAY_AND_OPENROUTER, models=_ROUTES)

    assert isinstance(built, OpenRouterModel)
    assert built.model_name == "openai/gpt-5.4-mini"
    assert built.profile.get("supports_json_object_output") is False
    assert built.settings is not None
    assert built.settings.get("openrouter_usage") == {"include": True}


def test_an_id_outside_the_catalog_needs_the_openrouter_credential() -> None:
    with pytest.raises(ValueError, match="openrouter"):
        models.model_for("openai/gpt-5.4-mini", infrastructure=_GATEWAY, models=_ROUTES)


def test_thinking_is_no_longer_a_deployment_setting() -> None:
    assert "thinking" not in ModelSettings.model_fields


def test_the_openai_id_builds_a_responses_model_on_the_openai_route() -> None:
    model = models.model_for("gpt-5.4", infrastructure=_GATEWAY, models=_ROUTES)

    assert isinstance(model, OpenAIResponsesModel)
    assert model.model_name == "gpt-5.4"
    assert model.provider is not None
    assert model.provider.base_url == "https://gateway.test/proxy/openai/"


def test_the_deepseek_route_name_comes_from_settings() -> None:
    model = models.model_for("deepseek-v4-pro-0813", infrastructure=_GATEWAY, models=ModelSettings(deepseek_gateway_route="DeepSeek1"))

    assert model.provider is not None
    assert model.provider.base_url == "https://gateway.test/proxy/DeepSeek1/"


def test_the_default_model_resolves_through_the_catalog() -> None:

    assert models.model(_GATEWAY_AND_OPENROUTER, _ROUTES).model_name == "z-ai/glm-5.3-flash"


def test_a_deepseek_response_is_priced_from_the_deepseek_table() -> None:
    response = ModelResponse(parts=[], model_name="deepseek-v4-flash-0731", usage=RequestUsage(input_tokens=1_000, output_tokens=500))

    price = models.price_response(response)

    assert price is not None
    assert price > 0
    assert price == calc_price(response.usage, model_ref="deepseek-v4-flash-0731", provider_id="deepseek").total_price


def test_other_responses_fall_back_to_the_registry() -> None:
    openai_response = ModelResponse(parts=[], model_name="gpt-5.4", usage=RequestUsage(input_tokens=10, output_tokens=1))

    assert models.price_response(openai_response) is None
    assert models.price_response(ModelResponse(parts=[], model_name=None)) is None


def test_an_openrouter_deepseek_response_prices_on_the_openrouter_route() -> None:
    """The same model sells for different amounts per route, so the route that answered prices it."""
    response = ModelResponse(
        parts=[],
        model_name="deepseek/deepseek-v4-pro-0813",
        provider_name="openrouter",
        usage=RequestUsage(input_tokens=1_000, output_tokens=500),
    )

    price = models.price_response(response)

    assert price is not None
    assert price > 0


def test_a_wire_name_outside_the_catalog_is_left_to_the_registry() -> None:
    """A prefix strip that finds no catalog entry must not price a model this deployment does not build."""
    response = ModelResponse(
        parts=[],
        model_name="deepseek/deepseek-v9-imaginary",
        provider_name="openrouter",
        usage=RequestUsage(input_tokens=1_000, output_tokens=500),
    )

    assert models.price_response(response) is None


class TestTheResponseCeiling:
    """Every model this app builds carries a ceiling on one response.

    A broker route holds credit against this number while a request is in flight, so an absent or
    oversized ceiling refuses requests the balance could have paid for.
    """

    def test_every_catalog_model_is_built_with_a_ceiling(self) -> None:
        for model_id in models.CATALOG:
            assert _ceiling_of(model_id), f"{model_id} was built with no response ceiling"

    def test_the_ceiling_is_the_one_the_deployment_declares(self) -> None:
        assert _ceiling_of(next(iter(models.CATALOG))) == _ROUTES.max_tokens


def test_a_route_prices_on_its_own_rate_not_the_vendors() -> None:
    # The same flash snapshot bills differently on each route, so a fallback that ignored the
    # answering provider would report one of them wrong.
    usage = RequestUsage(input_tokens=1_000_000, output_tokens=0)
    through_openrouter = ModelResponse(parts=[], model_name="deepseek/deepseek-v4-flash-0731", provider_name="openrouter", usage=usage)
    through_gateway = ModelResponse(parts=[], model_name="deepseek-v4-flash-0731", provider_name="openai", usage=usage)

    priced_openrouter = models.price_response(through_openrouter)
    priced_gateway = models.price_response(through_gateway)

    assert priced_openrouter is not None
    assert priced_gateway is not None
    assert priced_openrouter != priced_gateway
