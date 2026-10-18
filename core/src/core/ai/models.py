"""Model factories for pydantic-ai: Azure OpenAI generation, embeddings over Azure OpenAI or the gateway, and a test shortcut.

A plain `openai.AsyncOpenAI` client pointed at Azure's OpenAI-compatible
`/openai/v1/` endpoint, authenticated with an Entra ID bearer token
(`azure.identity`, no API keys), and a `"test-model"` shortcut that returns
pydantic-ai's `TestModel` so unit tests never touch the network.

This module reads no environment variables. The caller must pass the endpoint
and deployment explicitly.
"""

from __future__ import annotations

import asyncio
import contextlib

from typing import TYPE_CHECKING

import httpx2

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import DEFAULT_CONNECTION_LIMITS, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT, AsyncOpenAI
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.gateway import gateway_provider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.retries import AsyncHTTPX2TenacityTransport, RetryConfig, wait_retry_after
from tenacity import Future, RetryCallState, retry_if_exception_type, retry_if_result, stop_after_attempt, wait_exponential

from core.thinking import Thinking as Thinking  # noqa: PLC0414 (the redundant alias marks a re-export)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic_ai.models import Model
    from pydantic_ai.profiles import ModelProfile
    from pydantic_ai.settings import ModelSettings

    from core.settings import InfrastructureSettings

_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"
_TEST_MODEL_NAME = "test-model"
_EMBEDDING_TIMEOUT = httpx2.Timeout(30.0, connect=5.0)
_EMBEDDING_ATTEMPTS = 5
_EMBEDDING_MAX_WAIT_SECONDS = 20.0
_CHAT_ATTEMPTS = 4
_CHAT_MAX_WAIT_SECONDS = 20.0
_CHAT_BACKOFF = wait_exponential(multiplier=1.0, min=1.0, max=8.0)
_CONNECTION_FAILURES = (httpx2.NetworkError, httpx2.RemoteProtocolError, httpx2.ConnectTimeout)
"""The errors that end an attempt before a response begins, never a read timeout."""
_SDK_RETRIED_STATUSES = frozenset({httpx2.codes.REQUEST_TIMEOUT, httpx2.codes.CONFLICT, httpx2.codes.TOO_MANY_REQUESTS})


def _is_retryable_status(response: httpx2.Response) -> None:
    """Raise `httpx2.HTTPStatusError` for HTTP 429 and any 5xx, which the retry policy then retries."""
    if response.status_code == httpx2.codes.TOO_MANY_REQUESTS or response.status_code >= httpx2.codes.INTERNAL_SERVER_ERROR:
        response.raise_for_status()


def embedding_retry_config(*, sleep: Callable[[float], Awaitable[None]] | None = None) -> RetryConfig:
    """The retry policy of an embedding request.

    A request is retried on a connection error, a timeout, HTTP 429 and HTTP 5xx, five attempts in total. The wait before a
    retry grows exponentially from 1.5 seconds to at most 8, and a 429 with a `Retry-After` header waits that long, up to 20 seconds.
    When every attempt fails, the last error is raised.

    Args:
        sleep: Waits for the given seconds between attempts, or `None` for the real sleep. A test passes a recorder.

    Returns:
        The tenacity retry configuration for `AsyncHTTPX2TenacityTransport`.
    """
    config = RetryConfig(
        retry=retry_if_exception_type((httpx2.TransportError, httpx2.HTTPStatusError)),
        wait=wait_retry_after(wait_exponential(multiplier=1.5, max=8.0), max_wait=_EMBEDDING_MAX_WAIT_SECONDS),
        stop=stop_after_attempt(_EMBEDDING_ATTEMPTS),
        reraise=True,
    )
    if sleep is not None:
        config["sleep"] = sleep
    return config


def embedding_http_client(*, wrapped: httpx2.AsyncBaseTransport | None = None, config: RetryConfig | None = None) -> httpx2.AsyncClient:
    """An HTTP client for embedding requests that retries under `embedding_retry_config`.

    Args:
        wrapped: The transport that sends each attempt, or `None` for the real network transport.
        config: The retry policy, or `None` for `embedding_retry_config()`.

    Returns:
        The client.
    """
    transport = AsyncHTTPX2TenacityTransport(
        config if config is not None else embedding_retry_config(), wrapped=wrapped, validate_response=_is_retryable_status
    )
    return httpx2.AsyncClient(transport=transport, timeout=_EMBEDDING_TIMEOUT)


def _is_sdk_retried_response(response: httpx2.Response) -> bool:
    """Whether the OpenAI SDK would retry this response: HTTP 408, 409, 429 or any 5xx."""
    return response.status_code in _SDK_RETRIED_STATUSES or response.status_code >= httpx2.codes.INTERNAL_SERVER_ERROR


def _finished(state: RetryCallState) -> Future:
    """The outcome of the attempt that just ended, which tenacity sets before it waits, sleeps or gives up."""
    if state.outcome is None:
        msg = "tenacity asked about an attempt that has not ended"
        raise RuntimeError(msg)
    return state.outcome


def _response(outcome: Future) -> httpx2.Response | None:
    """The response an attempt returned, or `None` when it raised."""
    if outcome.failed:
        return None
    response = outcome.result()
    return response if isinstance(response, httpx2.Response) else None


def _wait_before_chat_retry(state: RetryCallState) -> float:
    """The response's `Retry-After` whole seconds up to 20, else the exponential backoff."""
    response = _response(_finished(state))
    retry_after = response.headers.get("retry-after", "") if response is not None else ""
    if retry_after.isdecimal() and int(retry_after) > 0:
        return min(float(retry_after), _CHAT_MAX_WAIT_SECONDS)
    return _CHAT_BACKOFF(state)


async def _close_retried_response(state: RetryCallState) -> None:
    """Close a response that is about to be retried, so its connection goes back to the pool."""
    response = _response(_finished(state))
    if response is not None:
        await response.aclose()


def _hand_back_last_outcome(state: RetryCallState) -> httpx2.Response:
    """Return the last response unread, or raise the last connection error."""
    outcome = _finished(state)
    response = _response(outcome)
    if response is not None:
        return response
    error = outcome.exception()
    if error is None:
        msg = "tenacity gave up on an attempt that neither returned a response nor raised"
        raise RuntimeError(msg)
    raise error


def chat_retry_config(*, sleep: Callable[[float], Awaitable[None]] | None = None) -> RetryConfig:
    """The retry policy of a model request: three retries of a failed connection, or of HTTP 408, 409, 429 and 5xx.

    A failed connection is one that ends before a response begins, never a read timeout. Waits grow from 1 to 8 seconds,
    or follow a `Retry-After` header in whole seconds up to 20. Once attempts run out, the last response is returned
    unread, or the last connection error is raised.

    Args:
        sleep: Waits for the given seconds between attempts, or `None` for the real sleep. A test passes a recorder.

    Returns:
        The tenacity retry configuration for `AsyncHTTPX2TenacityTransport`.
    """
    config = RetryConfig(
        retry=retry_if_exception_type(_CONNECTION_FAILURES) | retry_if_result(_is_sdk_retried_response),
        wait=_wait_before_chat_retry,
        stop=stop_after_attempt(_CHAT_ATTEMPTS),
        before_sleep=_close_retried_response,
        retry_error_callback=_hand_back_last_outcome,
    )
    if sleep is not None:
        config["sleep"] = sleep
    return config


class _SelfClosingAsyncClient(httpx2.AsyncClient):
    """An HTTP client that closes itself on the running event loop once nothing holds it."""

    def __del__(self) -> None:
        if self.is_closed:
            return
        # `get_running_loop` raises outside a running event loop, and the client is then left to the garbage collector.
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self.aclose())


def chat_http_client(*, wrapped: httpx2.AsyncBaseTransport | None = None, config: RetryConfig | None = None) -> httpx2.AsyncClient:
    """An HTTP client for model requests that retries under `chat_retry_config`, on the OpenAI SDK's own client settings.

    It keeps the SDK's timeout, connection limits and redirect following. It closes itself once nothing holds it.
    The `AsyncOpenAI` client it is handed to must retry nothing itself (`max_retries=0`), or the retries multiply.

    Args:
        wrapped: The transport that sends each attempt, or `None` for the real network transport.
        config: The retry policy, or `None` for `chat_retry_config()`.

    Returns:
        The client.
    """
    network = wrapped if wrapped is not None else httpx2.AsyncHTTPTransport(limits=DEFAULT_CONNECTION_LIMITS)
    transport = AsyncHTTPX2TenacityTransport(config if config is not None else chat_retry_config(), wrapped=network)
    return _SelfClosingAsyncClient(transport=transport, timeout=DEFAULT_TIMEOUT, follow_redirects=True)


def create_azure_openai_client(
    endpoint: str, *, http_client: httpx2.AsyncClient | None = None, max_retries: int = DEFAULT_MAX_RETRIES
) -> AsyncOpenAI:
    """Build an `AsyncOpenAI` client against the Azure `/openai/v1/` endpoint.

    The API key is an async Entra ID bearer-token provider that never blocks the event loop.

    Args:
        endpoint: The Azure resource endpoint, e.g.
            `https://<resource>.openai.azure.com`. The `/openai/v1/` suffix
            is appended if not already present.
        http_client: The HTTP client the requests go through, or `None` for the SDK's own.
        max_retries: How many times the SDK itself retries a failed request.

    Returns:
        The client, authenticated with an Entra ID bearer token.
    """
    token_provider = get_bearer_token_provider(DefaultAzureCredential(), _COGNITIVE_SERVICES_SCOPE)

    async def async_token_provider() -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, token_provider)

    base_url = endpoint.rstrip("/")
    if not base_url.endswith("/openai/v1"):
        base_url = f"{base_url}/openai/v1"
    return AsyncOpenAI(base_url=f"{base_url}/", api_key=async_token_provider, http_client=http_client, max_retries=max_retries)


def create_azure_model(
    deployment: str, *, endpoint: str, settings: ModelSettings | None = None, http_client: httpx2.AsyncClient | None = None
) -> Model:
    """Build a pydantic-ai `Model` for an Azure deployment, always on the Responses API.

    `deployment == "test-model"` returns `TestModel` and builds no client, so it needs no network or credentials.
    A given `http_client` does all the retrying, and the SDK retries nothing on top of it.

    Args:
        deployment: The Azure deployment name, or `"test-model"`.
        endpoint: The Azure resource endpoint.
        settings: The request settings the model carries, or `None` for none.
        http_client: The HTTP client the requests go through, such as `chat_http_client()`, or `None` for the SDK's own
            client and its own retries.

    Returns:
        `TestModel`, or an `OpenAIResponsesModel` backed by an Azure client.
    """
    if deployment == _TEST_MODEL_NAME:
        return TestModel()
    max_retries = DEFAULT_MAX_RETRIES if http_client is None else 0
    client = create_azure_openai_client(endpoint, http_client=http_client, max_retries=max_retries)
    return OpenAIResponsesModel(deployment, provider=OpenAIProvider(openai_client=client), settings=settings)


def create_gateway_model(
    model_name: str,
    *,
    api_key: str,
    base_url: str,
    route: str | None = None,
    use_responses: bool = True,
    profile: ModelProfile | None = None,
    settings: ModelSettings | None = None,
) -> Model:
    """Build a pydantic-ai `Model` routed through the Pydantic AI Gateway.

    The gateway proxies to the upstream provider (OpenAI by default, or a custom routing group named
    by `route`), so one gateway key + base URL fronts every model. `use_responses` picks the wire:
    the Responses API (so `openai_reasoning_effort` applies) for models that expose it, or Chat
    Completions for a route that does not.

    Args:
        model_name: The upstream model name the gateway proxies (e.g. `gpt-5.1`, `deepseek-v4-flash`).
        api_key: The Gateway API key (`pylf_...`).
        base_url: The regional gateway proxy base, e.g. `https://gateway-eu.pydantic.dev/proxy`.
        route: An optional gateway route / routing-group name (e.g. `DeepSeek1` for a custom
            provider), or `None` to use the provider's default route.
        use_responses: `True` builds an `OpenAIResponsesModel` (Responses API), `False` an
            `OpenAIChatModel` (Chat Completions).
        profile: The profile the Chat Completions model is built with, or `None` for the model's own.
            The Responses model always takes its own.
        settings: The request settings the model carries, or `None` for none.

    Returns:
        The resolved pydantic-ai `Model` routed through the gateway.
    """
    provider = gateway_provider("openai", api_key=api_key, base_url=base_url, route=route)
    if use_responses:
        return OpenAIResponsesModel(model_name, provider=provider, settings=settings)
    return OpenAIChatModel(model_name, provider=provider, profile=profile, settings=settings)


def create_model(spec: str, *, endpoint: str | None = None) -> Model:
    """Resolve a `Model` from a spec, the dispatch over the two backends.

    `"test-model"` resolves to `TestModel`, which needs no network. Any other name resolves to an Azure
    deployment, and `endpoint` is then required.

    Args:
        spec: `"test-model"` or an Azure deployment name.
        endpoint: The Azure endpoint (required for the Azure path).

    Returns:
        The resolved pydantic-ai `Model`.

    Raises:
        ValueError: If `endpoint` is missing for the Azure path.
    """
    if spec == _TEST_MODEL_NAME:
        return TestModel()
    if endpoint is None:
        msg = f"create_model({spec!r}, ...) requires an Azure `endpoint`"
        raise ValueError(msg)
    return create_azure_model(spec, endpoint=endpoint)


def create_embedding_model(deployment: str, *, infrastructure: InfrastructureSettings) -> OpenAIEmbeddingModel:
    """Build an `OpenAIEmbeddingModel` on the connection `infrastructure.embeddings` names.

    `azure_openai` reaches the Azure deployment with an Entra ID token, and `gateway` reaches the same model
    through the Pydantic AI Gateway with its key. Both retry a failed request on a connection error, a timeout,
    HTTP 429 and HTTP 5xx, up to five attempts. Each wait doubles from 1.5 seconds to at most 8, or follows the
    response's `Retry-After` up to 20 seconds. The caller wraps the model in pydantic-ai's `Embedder` with
    `EmbeddingSettings(dimensions=...)`.

    Args:
        deployment: The embedding deployment or model name.
        infrastructure: The shared infrastructure settings. Its `embeddings` picks the connection.

    Returns:
        The embedding model.

    Raises:
        ValueError: The chosen connection has no `infrastructure.azure_openai.endpoint` or no `infrastructure.gateway.api_key`.
    """
    if infrastructure.embeddings == "azure_openai":
        endpoint = infrastructure.azure_openai.endpoint
        if not endpoint:
            msg = "embeddings are set to azure_openai, which needs infrastructure.azure_openai.endpoint"
            raise ValueError(msg)
        client = create_azure_openai_client(endpoint, http_client=embedding_http_client(), max_retries=0)
        return OpenAIEmbeddingModel(deployment, provider=OpenAIProvider(openai_client=client))
    gateway = infrastructure.gateway
    if not gateway.api_key:
        msg = "embeddings are set to gateway, which needs infrastructure.gateway.api_key"
        raise ValueError(msg)
    provider = gateway_provider("openai", api_key=gateway.api_key, base_url=gateway.base_url, http_client=embedding_http_client())
    provider.client.max_retries = 0
    return OpenAIEmbeddingModel(deployment, provider=provider)
