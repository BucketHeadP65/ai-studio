"""Doubles for the broadcaster's two raw connections, so a test never opens one."""

from __future__ import annotations

import asyncio


class FakeNotifier:
    """Records every `(channel, payload)` instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.closed = False

    async def notify(self, channel: str, payload: str) -> None:
        """Record the send."""
        self.sent.append((channel, payload))

    async def close(self) -> None:
        """Mark it closed."""
        self.closed = True


class FakeListener:
    """Hands back payloads a test queued with `arrive`."""

    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def arrive(self, payload: str) -> None:
        """Queue one payload for the next `get`."""
        self._queue.put_nowait(payload)

    async def open(self) -> None:
        """Mark it open."""
        self.opened = True

    async def get(self) -> str:
        """The next queued payload, waiting for one."""
        return await self._queue.get()

    async def close(self) -> None:
        """Mark it closed."""
        self.closed = True
