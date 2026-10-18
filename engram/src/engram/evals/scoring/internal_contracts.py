"""What scoring needs from retrieval: one search, which ids name a page, and the source document each page digests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from core.ai.retrieval.contracts import SqlResult

    from engram.evals.shared.contracts import Search


class Searcher(Protocol):
    """Runs one search and answers its hit rows."""

    async def __call__(self, query: Search) -> SqlResult:
        """The hits for `query`.

        Args:
            query: The search.

        Returns:
            The hit rows, each with at least `id`, `hit_level` and `page_id`.
        """
        ...


class HitSources(Protocol):
    """Which ids name a page, and the source document each page names, the way its page record stores it."""

    async def page_ids(self, collection: str, ids: Sequence[str]) -> Collection[str]:
        """The ids among `ids` that name a page, with or without a source.

        Args:
            collection: The knowledge base the pages belong to.
            ids: The ids to look up.

        Returns:
            The ids that name a page in `collection`.
        """
        ...

    async def source_ids(self, collection: str, page_ids: Sequence[str]) -> Mapping[str, str]:
        """The source document of each page in `page_ids` that names one.

        Args:
            collection: The knowledge base the pages belong to.
            page_ids: The page ids to look up.

        Returns:
            The source id by page id, leaving out a page that names none or does not exist.
        """
        ...
