"""Every model this deployment can reach, on one list: the pinned catalog, then OpenRouter's tool-capable models.

The pinned catalog is what `models.CATALOG` proves a route for. The rest is OpenRouter's own list, fetched with the
key on file, narrowed to models that accept tools, and kept in process for an hour. A search is one case-insensitive
substring over id and name, pinned entries first.
"""

from __future__ import annotations

import time

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol

import httpx

from core.cqrs.contracts import utcnow
from genai_prices.data_snapshot import get_snapshot
from pydantic import BaseModel, ConfigDict, Field

from harness.adapters.models import CATALOG, context_window_for, openrouter_wire_name

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from genai_prices.types import ModelPrice, TieredPrices

Route = Literal["openrouter", "gateway", "azure"]
Reasoning = Literal["effort", "toggle", "none"]

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
REFRESH_SECONDS = 3600.0
SEARCH_LIMIT = 50


class ModelEntry(BaseModel, frozen=True):
    """One model the owner may put a tier on."""

    id: str
    name: str
    route: Route
    context_length: int | None
    price_in_per_m: Decimal | None
    price_out_per_m: Decimal | None
    reasoning: Reasoning
    pinned: bool


class ModelListing(Protocol):
    """Where the remote half of the list comes from."""

    async def fetch(self) -> Sequence[ModelEntry]:
        """Every remote model that accepts tools.

        Returns:
            The entries, in the remote list's own order.

        Raises:
            httpx.HTTPError: The list could not be fetched.
            ValueError: The list came back in a shape this app does not read.
        """
        ...


class _Pricing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt: Decimal
    completion: Decimal


class _Listed(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    context_length: int | None = None
    pricing: _Pricing
    supported_parameters: list[str] = Field(default_factory=list)


class _Page(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: list[_Listed]


def _reasoning(parameters: Sequence[str]) -> Reasoning:
    if "reasoning" in parameters:
        return "effort"
    if "include_reasoning" in parameters:
        return "toggle"
    return "none"


def _per_million(per_token: Decimal) -> Decimal:
    return per_token * 1_000_000


@dataclass(frozen=True)
class OpenRouterListing:
    """OpenRouter's model list under the key on file, kept to the models that accept tools."""

    api_key: str
    transport: httpx.AsyncBaseTransport | None = None

    async def fetch(self) -> list[ModelEntry]:
        """Fetch OpenRouter's one page of models and keep the ones that accept tools.

        Returns:
            The entries, in OpenRouter's own order, none of them pinned.

        Raises:
            httpx.HTTPError: OpenRouter could not be reached or answered a failure status.
            ValueError: The page is not in the shape this app reads.
        """
        async with httpx.AsyncClient(transport=self.transport, timeout=30.0) as client:
            response = await client.get(OPENROUTER_MODELS_URL, headers={"Authorization": f"Bearer {self.api_key}"})
        response.raise_for_status()
        page = _Page.model_validate(response.json())
        return [
            ModelEntry(
                id=listed.id,
                name=listed.name,
                route="openrouter",
                context_length=listed.context_length,
                price_in_per_m=_per_million(listed.pricing.prompt),
                price_out_per_m=_per_million(listed.pricing.completion),
                reasoning=_reasoning(listed.supported_parameters),
                pinned=False,
            )
            for listed in page.data
            if "tools" in listed.supported_parameters
        ]


@dataclass
class ModelList:
    """The merged list, refreshed from `listing` once per `REFRESH_SECONDS` and never blocking on a failure."""

    listing: ModelListing | None
    clock: Callable[[], float] = time.monotonic
    """The monotonic clock the refresh interval is measured on."""
    wall_clock: Callable[[], datetime] = utcnow
    """The current date and time, which picks the registry price in force for a pinned entry."""
    _remote: tuple[ModelEntry, ...] = field(default=(), init=False)
    _fetched_at: float | None = field(default=None, init=False)

    async def warm(self) -> None:
        """Fetch the remote half now when it is stale. A failure keeps the last list."""
        if self.listing is None:
            return
        now = self.clock()
        if self._fetched_at is not None and now - self._fetched_at < REFRESH_SECONDS:
            return
        try:
            self._remote = tuple(await self.listing.fetch())
        except (httpx.HTTPError, ValueError):
            return
        self._fetched_at = now

    async def entries(self) -> tuple[ModelEntry, ...]:
        """Every entry, fetching the remote half first when it is stale.

        Returns:
            The pinned entries first, then every remote entry that is not a pinned id's wire name.
        """
        await self.warm()
        return self._merged()

    async def search(self, q: str, *, limit: int = SEARCH_LIMIT) -> tuple[ModelEntry, ...]:
        """The entries whose id or name contains `q`, ignoring case, pinned first.

        Args:
            q: The text to look for. Surrounding whitespace is ignored.
            limit: The most entries to return.

        Returns:
            At most `limit` entries, in list order.
        """
        needle = q.strip().lower()
        hits = [entry for entry in await self.entries() if needle in entry.id.lower() or needle in entry.name.lower()]
        return tuple(hits[:limit])

    def is_listed(self, model_id: str) -> bool:
        """Whether `model_id` is a pinned id or a remote id from the last fetch, without fetching.

        Args:
            model_id: The model id to look for.

        Returns:
            True when the list in hand carries it.
        """
        return self.entry(model_id) is not None

    def entry(self, model_id: str) -> ModelEntry | None:
        """The entry for `model_id` in the list in hand now, without fetching.

        Args:
            model_id: The model id to look for.

        Returns:
            The entry, or `None` when the list in hand does not carry it.
        """
        return next((entry for entry in self._merged() if entry.id == model_id), None)

    def _merged(self) -> tuple[ModelEntry, ...]:
        at = self.wall_clock()
        by_wire = {entry.id: entry for entry in self._remote}
        pinned = tuple(_pinned(model_id, by_wire, on_openrouter=self.listing is not None, at=at) for model_id in CATALOG)
        wired = {openrouter_wire_name(model_id) for model_id in CATALOG}
        return (*pinned, *(entry for entry in self._remote if entry.id not in wired))


def _flat_price(value: Decimal | TieredPrices | None) -> Decimal | None:
    """A registry figure as a flat per-million rate, or `None` for a tiered one this list does not carry."""
    return value if isinstance(value, Decimal) else None


def _registry_price(wire: str, at: datetime) -> ModelPrice | None:
    """The price registry's OpenRouter price for `wire` in force at `at`, or `None` when it carries none."""
    try:
        _, model = get_snapshot().find_provider_model(wire, None, "openrouter", None)
    except LookupError:
        return None
    return model.get_prices(at)


def _pinned(model_id: str, remote: dict[str, ModelEntry], *, on_openrouter: bool, at: datetime) -> ModelEntry:
    """A catalog id as a list entry, with remote facts when listed, else the registry's price at `at`. `on_openrouter` decides its route."""
    wire = openrouter_wire_name(model_id)
    routed = on_openrouter and wire is not None
    azure = CATALOG[model_id].vendor == "azure"
    seen = remote.get(wire) if wire is not None else None
    registry = _registry_price(wire, at) if wire is not None else None
    return ModelEntry(
        id=model_id,
        name=seen.name if seen is not None else model_id,
        route="azure" if azure else ("openrouter" if routed else "gateway"),
        context_length=context_window_for(model_id),
        price_in_per_m=seen.price_in_per_m if seen is not None else (_flat_price(registry.input_mtok) if registry is not None else None),
        price_out_per_m=seen.price_out_per_m if seen is not None else (_flat_price(registry.output_mtok) if registry is not None else None),
        reasoning=seen.reasoning if seen is not None else "effort",
        pinned=True,
    )

