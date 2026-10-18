"""`web_search`: Tavily when a key is given, DuckDuckGo (keyless) otherwise, one name either way.

Both providers are read off the installed `pydantic_ai.common_tools` wrappers, so the only claims
worth proving here are this module's own: which provider a key selects, that the tool always
answers to `WEB_SEARCH_TOOL` regardless, and the developer cap. Nothing here calls a provider --
constructing `AsyncTavilyClient`/`DDGS` makes no network request, only searching would.
"""

from __future__ import annotations

from harness.adapters.websearch import MAX_RESULTS, WEB_SEARCH_TOOL, web_search_tool, web_search_toolset


def test_a_key_selects_tavily() -> None:
    tool = web_search_tool("a-fake-key")
    assert tool.name == WEB_SEARCH_TOOL
    assert "TavilySearchTool" in tool.function.__qualname__


def test_no_key_falls_back_to_duckduckgo() -> None:
    tool = web_search_tool(None)
    assert tool.name == WEB_SEARCH_TOOL
    assert "DuckDuckGoSearchTool" in tool.function.__qualname__


def test_an_empty_string_key_is_treated_as_no_key() -> None:
    """An unfilled env export is absent, not a credential -- the same reading `settings.py` gives every string export."""
    tool = web_search_tool("")
    assert "DuckDuckGoSearchTool" in tool.function.__qualname__


def test_the_tool_carries_a_description_whichever_provider_answered() -> None:
    assert web_search_tool("a-fake-key").description
    assert web_search_tool(None).description


def test_the_toolset_holds_exactly_the_one_tool() -> None:
    toolset = web_search_toolset(None)
    assert list(toolset.tools) == [WEB_SEARCH_TOOL]


def test_two_builds_with_the_same_key_share_one_instance() -> None:
    """Every mount site (a worker, a step call, a map item) builds this fresh.

    Without caching, each one would leak its own `AsyncTavilyClient` and the `httpx.AsyncClient`
    underneath it.
    """
    assert web_search_toolset("a-fake-key") is web_search_toolset("a-fake-key")
    assert web_search_toolset(None) is web_search_toolset(None)


def test_max_results_is_a_small_developer_set_cap() -> None:
    """Capped so one search cannot flood the context; not asserting the exact number, only its shape."""
    assert 0 < MAX_RESULTS <= 10
