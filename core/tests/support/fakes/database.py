"""A fake Postgres: records every statement with its arguments and answers with scripted rows."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class FakeConnection:
    """Records every statement in `executed`, and answers each fetch with the next scripted list of rows.

    A fetch past the end of the script answers with no rows at all.
    """

    def __init__(self, scripted: list[list[dict[str, object]]]) -> None:
        self.scripted = scripted
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.transactions: list[tuple[int, int]] = []
        """Every transaction's span: the `executed` indices it opened at and closed at, in the order they ran."""

    async def execute(self, query: str, *args: object) -> str:
        self.executed.append((query, args))
        return "OK"

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.executed.append((query, args))
        return self._rows()

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.executed.append((query, args))
        rows = self._rows()
        return rows[0] if rows else None

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None]:
        start = len(self.executed)
        yield
        self.transactions.append((start, len(self.executed)))

    def statements(self) -> list[str]:
        """Every statement run so far."""
        return [query for query, _ in self.executed]

    def _rows(self) -> list[dict[str, object]]:
        return self.scripted.pop(0) if self.scripted else []


class FakeDatabase:
    """Hands out the one connection for every block, so a test reads everything that ran off `opened`."""

    def __init__(self, results: list[list[dict[str, object]]] | None = None) -> None:
        self.opened = FakeConnection(list(results or []))

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[FakeConnection]:
        yield self.opened
