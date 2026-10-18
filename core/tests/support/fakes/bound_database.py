"""Bind a store to one already-acquired connection for transaction-interleaving tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from core.ai.history.contracts import Connection


@dataclass
class BoundDatabase:
    """A connection whose outer transaction is controlled by the test."""

    connection: Connection

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Connection]:
        """Reuse the acquired connection without committing its outer transaction."""
        yield self.connection
