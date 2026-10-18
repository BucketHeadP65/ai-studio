"""`HitSources` over the admin gateway: which ids name a page, and a page's source document, read from `kb.pages`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.ai.retrieval.contracts import SqlResult

_PAGES_SQL = "SELECT id FROM kb.pages WHERE collection = :collection AND id = ANY(CAST(:ids AS text[]))"

_SOURCES_SQL = (
    "SELECT id, source_id FROM kb.pages WHERE collection = :collection AND id = ANY(CAST(:ids AS text[])) AND source_id IS NOT NULL"
)


class _Rows(Protocol):
    """The one read this lookup needs of a gateway."""

    async def rows(self, sql: str, params: Mapping[str, object] | None = ..., *, cap: int = 50) -> SqlResult: ...


class AdminHitSources:
    """Reads each page's source document through the admin gateway, which can read `kb.pages`."""

    def __init__(self, gateway: _Rows) -> None:
        """Keep the gateway.

        Args:
            gateway: A gateway that can read the `kb` tables.
        """
        self._gateway = gateway

    async def page_ids(self, collection: str, ids: Sequence[str]) -> frozenset[str]:
        """Read which of `ids` are rows of `kb.pages`.

        Args:
            collection: The knowledge base the pages belong to.
            ids: The ids to look up.

        Returns:
            The ids that name a page.
        """
        if not ids:
            return frozenset()
        result = await self._gateway.rows(_PAGES_SQL, {"collection": collection, "ids": list(ids)}, cap=len(ids))
        return frozenset(str(row["id"]) for row in result.rows)

    async def source_ids(self, collection: str, page_ids: Sequence[str]) -> Mapping[str, str]:
        """Read `kb.pages.source_id` for the pages that name one.

        Args:
            collection: The knowledge base the pages belong to.
            page_ids: The page ids to look up.

        Returns:
            The source id by page id.
        """
        if not page_ids:
            return {}
        result = await self._gateway.rows(_SOURCES_SQL, {"collection": collection, "ids": list(page_ids)}, cap=len(page_ids))
        return {str(row["id"]): str(row["source_id"]) for row in result.rows}
