"""The web search tool: Tavily when an API key is given, DuckDuckGo (keyless) otherwise.

Every mount site must take the tool from this module, never from `pydantic_ai.common_tools`
directly. Either vendor's tool is named `web_search`, so a step's grant and a model's call never
name the vendor.
"""

from __future__ import annotations

from functools import cache

from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool
from pydantic_ai.common_tools.tavily import tavily_search_tool
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

WEB_SEARCH_TOOL = "web_search"
"""The tool's name wherever it is mounted, in the chat entry and in a step's grant alike."""

MAX_RESULTS = 5
"""The most search results one call returns."""


def web_search_tool(api_key: str | None) -> Tool[object]:
    """Build the web search tool: Tavily when `api_key` is given, DuckDuckGo (keyless) otherwise, capped at `MAX_RESULTS`.

    It is named `WEB_SEARCH_TOOL` whichever vendor answers. Each call with a key opens a Tavily
    client that is never closed, so a mount site takes `web_search_toolset` instead.

    Args:
        api_key: The Tavily API key. `None` or empty picks DuckDuckGo.

    Returns:
        The tool.
    """
    built = tavily_search_tool(api_key, max_results=MAX_RESULTS) if api_key else duckduckgo_search_tool(max_results=MAX_RESULTS)
    return Tool[object](built.function, name=WEB_SEARCH_TOOL, description=built.description, takes_ctx=built.takes_ctx)


@cache
def web_search_toolset(api_key: str | None) -> FunctionToolset[object]:
    """`web_search` as the one-tool toolset the chat entry and a step's grant both mount.

    It is cached per `api_key`, so every mount shares one toolset and one Tavily client for the
    life of the process. A caller may ask for it per worker, per step call or per map item.

    Args:
        api_key: The Tavily API key. `None` or empty picks DuckDuckGo.

    Returns:
        The shared toolset for that key.
    """
    return FunctionToolset[object]([web_search_tool(api_key)])
