"""Fakes for the agent-memory seams: scriptable ingest and cards, no DB or embedder required."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.contracts import IngestResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class FakeIngest:
    """Records every projection payload. Set `fail_next` to make the next call raise `RuntimeError`."""

    def __init__(self) -> None:
        self.calls: list[tuple[Mapping[str, object], str, bool]] = []
        self.fail_next = False

    async def __call__(self, payload: Mapping[str, object], *, collection: str, embed: bool = True) -> IngestResult:
        """Record the ingest, or raise once when `fail_next` is set.

        Args:
            payload: The page payload.
            collection: The collection to ingest into.
            embed: Whether the ingest embeds the page.

        Returns:
            A result for a page named `fake`.

        Raises:
            RuntimeError: When `fail_next` is set. It clears the flag.
        """
        if self.fail_next:
            self.fail_next = False
            msg = "projection unavailable"
            raise RuntimeError(msg)
        self.calls.append((payload, collection, embed))
        return IngestResult(page_id="fake", collection=collection)


class FakeCards:
    """Serves a scripted live-cards listing."""

    def __init__(self, rows: Sequence[tuple[str, str, str]] = ()) -> None:
        """Script the listing.

        Args:
            rows: The live cards every call answers.
        """
        self.rows = list(rows)

    async def __call__(self) -> Sequence[tuple[str, str, str]]:
        """Answer the scripted rows.

        Returns:
            The scripted rows, in order.
        """
        return tuple(self.rows)


class FakeSearchHits:
    """Serves a scripted list of hybrid hits and records every call's `query`, `prefix` and `limit`.

    The fake does no filtering. Script the hits as already scoped, and assert scoping against `calls`.
    """

    def __init__(self, hits: Sequence[tuple[str, str, float]] = ()) -> None:
        """Script the hits.

        Args:
            hits: The hits every call answers.
        """
        self.hits = list(hits)
        self.calls: list[tuple[str, str, int]] = []

    async def __call__(self, query: str, *, prefix: str, limit: int) -> Sequence[tuple[str, str, float]]:
        """Record the search and answer the scripted hits.

        Args:
            query: The search text.
            prefix: The page id prefix to filter to.
            limit: The most hits to return.

        Returns:
            The scripted hits, whatever the arguments.
        """
        self.calls.append((query, prefix, limit))
        return tuple(self.hits)
