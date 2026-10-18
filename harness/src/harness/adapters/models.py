"""The models this app runs on: the catalog of proven routes, the model factory over the gateway and OpenRouter, and response pricing."""

from __future__ import annotations

import re

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from core.ai.models import Thinking, chat_http_client, create_azure_model, create_gateway_model
from genai_prices import calc_price
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings as ModelRequestSettings
from pydantic_ai_harness.compaction import resolve_context_window

if TYPE_CHECKING:
    from core.settings import InfrastructureSettings
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.models import Model

    from harness.settings import ModelSettings

_DATED_SNAPSHOT = re.compile(r"-\d{4}$")
"""The snapshot date a model id may end with, such as `-0731`, which a price lookup drops."""

MODEL_ID = "glm-5.3-flash"
"""The default model id: the one `model` builds, and the one every tier runs on until the owner sets another."""


@dataclass(frozen=True)
class ModelRoute:
    """How one model id is reached.

    `vendor` picks the route and the profile. `openai` is the gateway's own OpenAI route. `deepseek` is the DeepSeek
    route the `deepseek_gateway_route` setting names, or OpenRouter when an OpenRouter key is on file. `zai` is Z.ai's
    GLM family, reachable through OpenRouter only. `azure` is an Azure OpenAI deployment of the same name, reached at
    `infrastructure.azure_openai.endpoint` with an Entra ID token, always on the Responses API. `api` is the wire the model
    speaks through the gateway. DeepSeek
    speaks chat completions only, because the gateway refuses it on the responses endpoint.
    """

    vendor: Literal["openai", "deepseek", "zai", "azure"]
    api: Literal["chat", "responses"]


CATALOG: dict[str, ModelRoute] = {
    MODEL_ID: ModelRoute(vendor="zai", api="chat"),
    "deepseek-v4-pro-0813": ModelRoute(vendor="deepseek", api="chat"),
    "deepseek-v4-flash-0731": ModelRoute(vendor="deepseek", api="chat"),
    "gpt-5.4": ModelRoute(vendor="openai", api="responses"),
    "gpt-6-luna": ModelRoute(vendor="azure", api="responses"),
}
"""Every model id with a proven route here, the default first. `model_for` builds any other id as an OpenRouter id.

Every DeepSeek id must carry its snapshot date. An undated id resolves to an April snapshot, not to the newest model.
"""


_OPENROUTER_PREFIX: dict[str, str] = {"deepseek": "deepseek", "zai": "z-ai"}
"""How OpenRouter prefixes each vendor's model ids on its wire. A vendor absent here is never routed there."""

_OPENROUTER_HOSTS_NEVER_USED: tuple[str, ...] = ("inference-net",)
"""The OpenRouter hosts no request is sent to, by the provider slug OpenRouter's endpoint list gives.

InferenceNet sends most tool calls with empty arguments, for GLM 5.3 Flash and DeepSeek V4.1 Flash alike.
"""

_OPENROUTER_IGNORED_HOSTS: dict[str, tuple[str, ...]] = {"z-ai/glm-5.3-flash": ("together",)}
"""The further OpenRouter hosts each wire name is never sent to, by provider slug.

Together bills some GLM 5.3 Flash replies whose stream carries no text, no reasoning and no tool call. Once one such reply
sits in a turn's history, the retries that follow mostly come back empty too, and the turn fails.
"""


def context_window_for(model_id: str) -> int | None:
    """How many tokens `model_id` accepts.

    The price registry is asked under the model's OpenRouter name first, when it has one, then under the vendor's own
    name, with the snapshot date dropped both times.

    Args:
        model_id: A catalog model id.

    Returns:
        The window, or `None` when the registry does not know it or the id is outside the catalog.
    """
    entry = CATALOG.get(model_id)
    if entry is None:
        return None
    family = _DATED_SNAPSHOT.sub("", model_id)
    prefix = _OPENROUTER_PREFIX.get(entry.vendor)
    candidates = [*([f"openrouter:{prefix}/{family}"] if prefix is not None else []), f"{entry.vendor}:{family}"]
    for candidate in candidates:
        window = resolve_context_window(candidate)
        if window is not None:
            return window
    return None


def _deepseek_profile(model_id: str, *, own: ModelProfile | None) -> ModelProfile:
    """DeepSeek's own model facts for `model_id`, merged over `own`, the profile of whichever route serves it."""
    # DeepSeek takes `json_object` output only, and v4 and the reasoner refuse `tool_choice=required`. OpenRouter's
    # profile for a `deepseek/` name states neither. Pin nothing else, or the gateway's field names reach OpenRouter.
    base: ModelProfile = own if own is not None else ModelProfile()
    return merge_profile(
        base,
        ModelProfile(
            supports_json_schema_output=False,
            supports_json_object_output=True,
            supports_forced_tool_choice=(model_id != "deepseek-reasoner" and not model_id.startswith("deepseek-v4-")),
        ),
    )


def openrouter_wire_name(model_id: str) -> str | None:
    """The name OpenRouter knows `model_id` by, the one place that spelling is decided.

    Everywhere else this deployment says the bare catalog id.

    Args:
        model_id: A catalog model id.

    Returns:
        `<prefix>/<model_id>`, or `None` for an id outside the catalog or a vendor OpenRouter does not carry.
    """
    entry = CATALOG.get(model_id)
    if entry is None:
        return None
    prefix = _OPENROUTER_PREFIX.get(entry.vendor)
    return None if prefix is None else f"{prefix}/{model_id}"


DEFAULT_THINKING: Thinking = "xhigh"
"""The level a model builds at when nothing more specific names one."""


def _openrouter_model(
    wire_name: str,
    *,
    vendor: Literal["openai", "deepseek", "zai", "azure"] | None,
    thinking: Thinking,
    provider: OpenRouterProvider,
    ceiling: int,
) -> OpenRouterModel:
    """One model reached through OpenRouter at `thinking`, with DeepSeek's pins when `vendor` is `deepseek`, else its own profile."""
    own = OpenRouterProvider.model_profile(wire_name)
    profile = (
        _deepseek_profile(wire_name.rpartition("/")[2], own=own) if vendor == "deepseek" else (own if own is not None else ModelProfile())
    )
    # OpenRouter's marketplace hosts silently drop tool calls when a request asks for `json_object` output, so on
    # this route alone prompted output keeps its schema in the prompt and sends no `response_format`.
    profile = merge_profile(profile, OpenAIModelProfile(supports_json_object_output=False))
    # `usage.include` makes OpenRouter report what it billed on every response, which `billed_cost` reads ahead of
    # any table. Without it the response carries token counts and no figure.
    openrouter_settings = OpenRouterModelSettings(thinking=thinking, max_tokens=ceiling, openrouter_usage={"include": True})
    openrouter_settings["openrouter_provider"] = {"ignore": [*_OPENROUTER_IGNORED_HOSTS.get(wire_name, ()), *_OPENROUTER_HOSTS_NEVER_USED]}
    return OpenRouterModel(wire_name, provider=provider, profile=profile, settings=openrouter_settings)


def model_for(model_id: str, *, infrastructure: InfrastructureSettings, models: ModelSettings, thinking: Thinking = DEFAULT_THINKING) -> Model:
    """The named model at `thinking`: a catalog id on its proven route (OpenRouter, the gateway or Azure), any other id as an OpenRouter id.

    A catalog id reached through OpenRouter is named `<prefix>/<model_id>` on the wire. An id outside the catalog is
    handed to OpenRouter as it is, with OpenRouter's own profile for it. No OpenRouter model here asks for `json_object`
    output, no OpenRouter model is sent to the InferenceNet host, and GLM 5.3 Flash is never sent to the Together host. The
    id is not checked against the model list here.

    Args:
        model_id: A catalog id, or any id OpenRouter lists.
        infrastructure: The infrastructure section, read for its `openrouter`, `gateway` and `azure_openai` keys.
        models: The model settings, read for the output token ceiling and the DeepSeek gateway route.
        thinking: The thinking level the model is built at.

    Returns:
        The model.

    Raises:
        ValueError: The route `model_id` needs has no key in the settings file's infrastructure section.
    """
    entry = CATALOG.get(model_id)
    openrouter_key = infrastructure.openrouter.api_key
    if entry is None:
        if not openrouter_key:
            msg = f"{model_id!r} is reachable through OpenRouter only, and the settings file carries no infrastructure.openrouter key."
            raise ValueError(msg)
        provider = OpenRouterProvider(api_key=openrouter_key)
        return _openrouter_model(model_id, vendor=None, thinking=thinking, provider=provider, ceiling=models.max_tokens)
    request_settings = ModelRequestSettings(thinking=thinking, max_tokens=models.max_tokens)
    if entry.vendor == "azure":
        endpoint = infrastructure.azure_openai.endpoint
        if not endpoint:
            msg = f"{model_id!r} is an Azure deployment, and the settings file carries no infrastructure.azure_openai.endpoint."
            raise ValueError(msg)
        return create_azure_model(model_id, endpoint=endpoint, settings=request_settings, http_client=chat_http_client())
    wire_name = openrouter_wire_name(model_id)
    if wire_name is not None and openrouter_key:
        provider = OpenRouterProvider(api_key=openrouter_key)
        return _openrouter_model(wire_name, vendor=entry.vendor, thinking=thinking, provider=provider, ceiling=models.max_tokens)
    if entry.vendor == "zai":
        msg = f"{model_id!r} is reachable through OpenRouter only, and the settings file carries no infrastructure.openrouter key."
        raise ValueError(msg)
    gateway = infrastructure.gateway
    if not gateway.api_key:
        msg = f"{model_id!r} is reached through the gateway, and the settings file carries no infrastructure.gateway key."
        raise ValueError(msg)
    route = models.deepseek_gateway_route if entry.vendor == "deepseek" else "openai"
    chat = entry.api == "chat"
    profile = _deepseek_profile(model_id, own=DeepSeekProvider.model_profile(model_id)) if chat and entry.vendor == "deepseek" else None
    return create_gateway_model(
        model_id,
        api_key=gateway.api_key,
        base_url=gateway.base_url,
        route=route,
        use_responses=not chat,
        profile=profile,
        settings=request_settings,
    )


@dataclass(frozen=True)
class ModelFactory:
    """`model_for` with this deployment's infrastructure and route settings already bound: how a tier name becomes a model here."""

    infrastructure: InfrastructureSettings
    models: ModelSettings

    def __call__(self, model_id: str, /, *, thinking: Thinking = DEFAULT_THINKING) -> Model:
        """Build the model with this factory's infrastructure and model settings.

        Args:
            model_id: A catalog id, or any id OpenRouter lists.
            thinking: The thinking level the model is built at.

        Returns:
            The model.

        Raises:
            ValueError: The route `model_id` needs has no key in the infrastructure section.
        """
        return model_for(model_id, infrastructure=self.infrastructure, models=self.models, thinking=thinking)


def price_response(response: ModelResponse) -> Decimal | None:
    """What a catalog DeepSeek or GLM response cost by the price registry under its catalog name, or `None`.

    A DeepSeek response is looked up under the provider that answered, then under `deepseek`, which covers the gateway
    answering as `openai` under the bare catalog id. Only the snapshot date is dropped first, so
    `deepseek-v4-flash-0731` prices as `deepseek-v4-flash` and a vendor prefix stays. A GLM response is looked up under
    its wire name on the route that answered. What OpenRouter billed is read by `billed_cost`, ahead of this.

    Args:
        response: A model response.

    Returns:
        The price, or `None` when the response is not from a catalog DeepSeek or GLM model, or the registry does not price it.
    """
    name = response.model_name
    if not name:
        return None
    bare = name.rpartition("/")[2]
    entry = CATALOG.get(bare)
    if entry is None:
        return None
    if entry.vendor == "zai":
        return _price_zai(response, name)
    if entry.vendor != "deepseek":
        return None
    # The route that answered first, because a model sells for a different amount on each. Its own vendor second,
    # for a route the registry lists no model under, such as a gateway answering as `openai`.
    for model_ref, provider in ((_DATED_SNAPSHOT.sub("", name), response.provider_name), (_DATED_SNAPSHOT.sub("", bare), entry.vendor)):
        if provider is None:
            continue
        try:
            return calc_price(
                response.usage, model_ref=model_ref, provider_id=provider, genai_request_timestamp=response.timestamp
            ).total_price
        except LookupError:
            continue
    return None


def _price_zai(response: ModelResponse, wire_name: str) -> Decimal | None:
    """A GLM response's price by the registry under the route that answered, `None` when the registry does not price it there."""
    provider = response.provider_name
    if provider is None:
        return None
    try:
        return calc_price(response.usage, model_ref=wire_name, provider_id=provider, genai_request_timestamp=response.timestamp).total_price
    except LookupError:
        return None


def billed_cost(response: ModelResponse) -> Decimal | None:
    """The amount the provider says it billed for `response`.

    Read from `usage.cost` when the provider fills it, else from `provider_details["cost"]`, where the OpenRouter route
    reports what the answering host charged.

    Args:
        response: A model response.

    Returns:
        The billed amount, or `None` when the provider reported none.
    """
    if response.usage.cost is not None:
        return response.usage.cost
    details = response.provider_details
    if details is None:
        return None
    cost = details.get("cost")
    return Decimal(str(cost)) if isinstance(cost, int | float) and not isinstance(cost, bool) else None


def priced_cost(response: ModelResponse) -> Decimal | None:
    """What `response` cost, from the first source that can name a figure.

    The provider's billed figure (`billed_cost`) comes first, then the catalog prices (`price_response`), then the
    registry under the response's own provider. `None` is not zero. A caller that shows money to a person must show an
    unknown cost as unknown, never as free.

    Args:
        response: A model response.

    Returns:
        The cost, or `None` when no source can name one.
    """
    try:
        billed = billed_cost(response)
        if billed is not None:
            return billed
        own = price_response(response)
        if own is not None:
            return own
        return calc_price(
            response.usage,
            model_ref=response.model_name or "",
            provider_id=response.provider_name,
            genai_request_timestamp=response.timestamp,
        ).total_price
    except Exception:  # noqa: BLE001 (an unpriceable response is unknown, not an error, and both pricing sources raise many shapes)
        return None


def response_cost(response: ModelResponse) -> Decimal:
    """What `response` cost, counting an unknown cost as zero. For the spend brake, never for a screen.

    Anything that shows money to a person uses `priced_cost` instead, so an unknown cost is said rather than shown as
    free.

    Args:
        response: A model response.

    Returns:
        The cost, zero when unknown.
    """
    return priced_cost(response) or Decimal(0)


def model(infrastructure: InfrastructureSettings, models: ModelSettings) -> Model:
    """The one model this app runs on by default, `MODEL_ID` at the default thinking level.

    Args:
        infrastructure: The infrastructure section its route is reached with.
        models: The model settings its route is built from.

    Returns:
        The model.
    """
    return model_for(MODEL_ID, infrastructure=infrastructure, models=models)
