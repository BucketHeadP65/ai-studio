"""The model list: pinned catalog first, OpenRouter's tool-capable models behind it, one search, an hourly refresh."""

from __future__ import annotations

from decimal import Decimal

import httpx

from core.settings import DatabaseSettings, InfrastructureSettings, OpenRouterSettings

from harness.adapters.model_list import ModelEntry, ModelList, OpenRouterListing, Reasoning
from harness.adapters.wiring import model_list
from tests.support.fakes.model_listing import FakeModelListing
from tests.support.price_registry import empty_price_registry


def _remote(model_id: str, name: str = "", *, reasoning: Reasoning = "effort") -> ModelEntry:
    return ModelEntry(
        id=model_id,
        name=name or model_id,
        route="openrouter",
        context_length=128_000,
        price_in_per_m=Decimal("0.5"),
        price_out_per_m=Decimal("1.5"),
        reasoning=reasoning,
        pinned=False,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_with_no_listing_the_list_is_the_pinned_catalog_alone() -> None:
    listed = ModelList(listing=None)

    entries = await listed.entries()

    assert [entry.id for entry in entries] == ["glm-5.3-flash", "deepseek-v4-pro-0813", "deepseek-v4-flash-0731", "gpt-5.4", "gpt-6-luna"]
    assert all(entry.pinned for entry in entries)


async def test_with_no_listing_a_pinned_entry_is_priced_by_the_registry_or_not_at_all() -> None:
    priced = await ModelList(listing=None).entries()
    with empty_price_registry():
        unpriced = await ModelList(listing=None).entries()

    assert priced[0].id == unpriced[0].id == "glm-5.3-flash"
    assert priced[0].price_in_per_m is not None
    assert priced[0].price_out_per_m is not None
    assert unpriced[0].price_in_per_m is None
    assert unpriced[0].price_out_per_m is None


async def test_with_no_openrouter_credential_every_pinned_entry_routes_through_the_gateway() -> None:
    listed = ModelList(listing=None)

    entries = await listed.entries()

    routes = {entry.id: entry.route for entry in entries}
    assert routes.pop("gpt-6-luna") == "azure"
    assert set(routes.values()) == {"gateway"}


async def test_with_an_openrouter_listing_the_openrouter_vendors_pinned_entries_route_there() -> None:
    """A list with a remote half is a deployment that can reach OpenRouter, which is what decides a pinned route."""
    listed = ModelList(listing=FakeModelListing())

    entries = await listed.entries()

    routes = {entry.id: entry.route for entry in entries}
    assert routes["glm-5.3-flash"] == "openrouter"
    assert routes["deepseek-v4-pro-0813"] == "openrouter"
    assert routes["deepseek-v4-flash-0731"] == "openrouter"
    assert routes["gpt-5.4"] == "gateway"
    assert routes["gpt-6-luna"] == "azure"


async def test_pinned_entries_come_first_and_remote_ones_follow() -> None:
    listing = FakeModelListing(entries=[_remote("openai/gpt-5.4-mini", "OpenAI: GPT 5.4 mini")])
    listed = ModelList(listing=listing)

    entries = await listed.entries()

    assert entries[0].pinned
    assert entries[0].id == "glm-5.3-flash"
    assert entries[-1].id == "openai/gpt-5.4-mini"
    assert not entries[-1].pinned


async def test_a_remote_entry_that_is_a_pinned_ids_wire_name_prices_the_pinned_entry() -> None:
    listing = FakeModelListing(entries=[_remote("z-ai/glm-5.3-flash", "Z.ai: GLM 5.3 Flash")])
    listed = ModelList(listing=listing)

    entries = await listed.entries()

    glm = next(entry for entry in entries if entry.id == "glm-5.3-flash")
    assert glm.price_in_per_m == Decimal("0.5")
    assert glm.name == "Z.ai: GLM 5.3 Flash"
    assert "z-ai/glm-5.3-flash" not in [entry.id for entry in entries]


async def test_search_is_a_case_insensitive_substring_over_id_and_name_pinned_first() -> None:
    listing = FakeModelListing(entries=[_remote("z-ai/glm-4.7", "Z.ai: GLM 4.7"), _remote("openai/gpt-5.4-mini", "GPT")])
    listed = ModelList(listing=listing)

    hits = await listed.search("GLM")

    assert [hit.id for hit in hits] == ["glm-5.3-flash", "z-ai/glm-4.7"]


async def test_search_answers_at_most_the_limit() -> None:
    listing = FakeModelListing(entries=[_remote(f"vendor/model-{n}") for n in range(80)])
    listed = ModelList(listing=listing)

    hits = await listed.search("model", limit=50)

    assert len(hits) == 50


async def test_the_remote_list_is_fetched_once_an_hour() -> None:
    clock = _Clock()
    listing = FakeModelListing(entries=[_remote("openai/gpt-5.4-mini")])
    listed = ModelList(listing=listing, clock=clock)

    await listed.entries()
    clock.now += 1800
    await listed.entries()
    assert listing.fetches == 1
    clock.now += 1801
    await listed.entries()
    assert listing.fetches == 2


async def test_a_failed_refresh_keeps_the_last_list_and_the_pinned_catalog() -> None:
    clock = _Clock()
    listing = FakeModelListing(entries=[_remote("openai/gpt-5.4-mini")])
    listed = ModelList(listing=listing, clock=clock)
    await listed.entries()
    listing.failure = httpx.ConnectError("down")
    clock.now += 4000

    entries = await listed.entries()

    assert "openai/gpt-5.4-mini" in [entry.id for entry in entries]


async def test_is_listed_knows_pinned_ids_before_any_fetch_and_remote_ids_after_one() -> None:
    listing = FakeModelListing(entries=[_remote("openai/gpt-5.4-mini")])
    listed = ModelList(listing=listing)

    assert listed.is_listed("glm-5.3-flash")
    assert not listed.is_listed("openai/gpt-5.4-mini")
    await listed.warm()
    assert listed.is_listed("openai/gpt-5.4-mini")
    assert not listed.is_listed("nobody/nothing")


async def test_the_openrouter_listing_keeps_tool_capable_models_and_reads_their_facts() -> None:
    payload = {
        "data": [
            {
                "id": "z-ai/glm-4.7",
                "name": "Z.ai: GLM 4.7",
                "context_length": 200000,
                "pricing": {"prompt": "0.0000004", "completion": "0.0000016"},
                "supported_parameters": ["tools", "reasoning", "include_reasoning"],
            },
            {
                "id": "vendor/chat-only",
                "name": "No tools",
                "context_length": 8000,
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_parameters": ["temperature"],
            },
            {
                "id": "vendor/toggle",
                "name": "Toggle",
                "context_length": 8000,
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_parameters": ["tools", "include_reasoning"],
            },
        ]
    }
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    listing = OpenRouterListing(api_key="test-key", transport=httpx.MockTransport(handle))

    entries = await listing.fetch()

    assert seen[0].headers["Authorization"] == "Bearer test-key"
    assert [entry.id for entry in entries] == ["z-ai/glm-4.7", "vendor/toggle"]
    glm = entries[0]
    assert glm.price_in_per_m == Decimal("0.4")
    assert glm.price_out_per_m == Decimal("1.6")
    assert glm.context_length == 200000
    assert glm.reasoning == "effort"
    assert glm.route == "openrouter"
    assert entries[1].reasoning == "toggle"


_DATABASE = DatabaseSettings(url="postgresql+asyncpg://owner:pw@db.example:5432/kb", reader_url="postgresql+asyncpg://reader:pw@db.example:5432/kb")


def test_the_list_has_no_remote_half_without_an_openrouter_key() -> None:
    assert model_list(InfrastructureSettings(database=_DATABASE)).listing is None


def test_the_key_on_file_is_what_the_remote_half_fetches_with() -> None:
    built = model_list(InfrastructureSettings(database=_DATABASE, openrouter=OpenRouterSettings(api_key="a-key")))

    assert built.listing == OpenRouterListing(api_key="a-key")
