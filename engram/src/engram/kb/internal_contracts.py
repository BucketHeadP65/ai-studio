"""The ports the knowledge base slice reads through."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.retrieval.contracts import SqlResult


class IndexedSchemaQueries(Protocol):
    """The slice of a read gateway `load_indexed_schema` needs: one capped SQL read.

    A host can pass any gateway with this method unchanged.
    """

    async def rows(self, sql: str, params: Mapping[str, object] | None = ..., *, cap: int = 50) -> SqlResult:
        """Run `sql` with bound `params`, returning at most `cap` rows.

        Args:
            sql: The read-only SQL to run.
            params: The values bound into `sql`, or None for none.
            cap: The most rows to return.

        Returns:
            The rows read, at most `cap` of them.
        """
        ...
