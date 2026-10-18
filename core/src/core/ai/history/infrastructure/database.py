"""Where the record's connections come from."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import asyncpg

    from core.ai.history.internal_contracts import Connection


@dataclass(frozen=True)
class PoolDatabase:
    """The record's database over an asyncpg pool."""

    pool: asyncpg.Pool

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Connection]:
        """Take a pooled connection for the duration of the block.

        Yields:
            The connection, given back to the pool when the block ends.
        """
        # asyncpg installs the connection's methods on its pooled proxy at class creation, so a type
        # checker sees none of them there. The pool hands back what it documents: a connection.
        async with self.pool.acquire() as pooled:
            yield cast("Connection", pooled)
