"""The two reads the memory store asks its host to answer over the agent-memory knowledge base."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class CardRows(Protocol):
    """Read the live cards of the agent-memory knowledge base."""

    async def __call__(self) -> Sequence[tuple[str, str, str]]:
        """Return every live page that nothing supersedes.

        Returns:
            `(page_id, title, summary)` for each page, in no promised order.
        """
        ...


class SearchHits(Protocol):
    """Rank the agent-memory knowledge base's pages against a query, within one store path prefix."""

    async def __call__(self, query: str, *, prefix: str, limit: int) -> Sequence[tuple[str, str, float]]:
        """Return the best-ranked pages under a store path prefix.

        The prefix filter must run before the ranking's own top-N cutoff, so pages from other scopes never crowd
        out this one. Callers trust the hits are already scoped and do not filter them again.

        Args:
            query: The search text.
            prefix: A store path prefix such as `"global/main/"`, not a page id. `page_id_of` turns it into an
                id prefix.
            limit: The most hits to return.

        Returns:
            `(page_id, snippet, score)` for each hit, best first.
        """
        ...
