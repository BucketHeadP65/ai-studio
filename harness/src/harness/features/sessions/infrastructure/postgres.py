"""The projection's database port over an asyncpg pool."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence

    import asyncpg

    from asyncpg.pool import PoolConnectionProxy

    from harness.features.sessions.internal_contracts import ProjectionPool


@dataclass(frozen=True)
class PostgresProjectionPool:
    """The projection's port over a real pool. Every statement outside a transaction runs on a connection the pool lends for it."""

    pool: asyncpg.Pool
    """The pool this deployment opened onto its projection database."""

    async def fetch(self, query: str, *args: object) -> list[Mapping[str, object]]:
        """Run one statement on a lent connection and read every row it answers with.

        Args:
            query: The statement.
            *args: The values its numbered placeholders take.

        Returns:
            The rows, in the order the statement answered with them.
        """
        return await self.pool.fetch(query, *args)

    async def fetchrow(self, query: str, *args: object) -> Mapping[str, object] | None:
        """Run one statement on a lent connection and read the first row it answers with.

        Args:
            query: The statement.
            *args: The values its numbered placeholders take.

        Returns:
            The first row, or `None` when the statement answers with none.
        """
        return await self.pool.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: object) -> object:
        """Run one statement on a lent connection and read the first column of its first row.

        Args:
            query: The statement.
            *args: The values its numbered placeholders take.

        Returns:
            The value, or `None` when the statement answers with no row.
        """
        return await self.pool.fetchval(query, *args)

    async def execute(self, query: str, *args: object) -> str:
        """Run one statement on a lent connection.

        Args:
            query: The statement.
            *args: The values its numbered placeholders take.

        Returns:
            The status the database answered with.
        """
        return await self.pool.execute(query, *args)

    async def executemany(self, query: str, args: Sequence[Sequence[object]]) -> object:
        """Run one statement once per row of values, on a lent connection.

        Args:
            query: The statement.
            args: One sequence of placeholder values per run.

        Returns:
            What the database answered with.
        """
        return await self.pool.executemany(query, args)

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[ProjectionPool]:
        """Take one connection out of the pool with a transaction open on it, released when the block ends.

        Yields:
            The connection, under the same port the pool answers.
        """
        async with self.pool.acquire() as connection, connection.transaction():
            yield _PostgresTransaction(connection)


@dataclass(frozen=True)
class _PostgresTransaction:
    """The one connection a transaction is open on, under the same port the pool answers."""

    connection: PoolConnectionProxy
    """The connection the pool lent, held only for the length of the transaction."""

    async def fetch(self, query: str, *args: object) -> list[Mapping[str, object]]:
        """Every row `query` answers, inside this transaction."""
        return await self.connection.fetch(query, *args)

    async def fetchrow(self, query: str, *args: object) -> Mapping[str, object] | None:
        """`query`'s first row inside this transaction, or `None`."""
        return await self.connection.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: object) -> object:
        """The first column of `query`'s first row inside this transaction, `None` for no match."""
        return await self.connection.fetchval(query, *args)

    async def execute(self, query: str, *args: object) -> str:
        """Run one statement inside this transaction."""
        return await self.connection.execute(query, *args)

    async def executemany(self, query: str, args: Sequence[Sequence[object]]) -> object:
        """Run one statement once per row of `args`, inside this transaction."""
        return await self.connection.executemany(query, args)

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[ProjectionPool]:
        """The transaction already open on this connection: what runs here is already inside one."""
        yield self
