"""The HTTP client this app reaches the studio knowledge base API through.

This app must never import `studio`. Two callers use this client. `digest_now` lands a source and
starts a refresh run, and the knowledge base plugins read the registry of knowledge bases
(`adapters.knowledge_base.registered_knowledge_bases`), which the studio owns. The tools over one
knowledge base come from `studio mcp` instead (`adapters.knowledge_base.knowledge_base_stdio_transport`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from harness.settings import StudioApiSettings

INGEST_TIMEOUT_SECONDS = 300.0
"""How long the client waits for an answer, long enough for a sync-then-index run over a real backlog to finish."""


class StudioApi(Protocol):
    """The part of the studio knowledge base API client a reader here uses: one GET, and where it points.

    `httpx.AsyncClient` satisfies it. A reader that refuses a studio that does not answer names it by `base_url`.
    """

    @property
    def base_url(self) -> httpx.URL:
        """Where this client points: the studio knowledge base API this deployment reads."""
        ...

    async def get(self, url: str) -> httpx.Response:
        """GET `url` under `base_url`.

        Args:
            url: A path under `base_url`.

        Returns:
            The response, whatever its status.

        Raises:
            httpx.RequestError: The studio did not answer at all.
        """
        ...


def studio_http_client(settings: StudioApiSettings) -> httpx.AsyncClient:
    """Build one client on the studio knowledge base API `settings` names, waiting `INGEST_TIMEOUT_SECONDS` for an answer.

    Args:
        settings: The studio API section, whose `base_url` the client points at.

    Returns:
        The client. The caller closes it.
    """
    return httpx.AsyncClient(base_url=settings.base_url, timeout=httpx.Timeout(INGEST_TIMEOUT_SECONDS))
