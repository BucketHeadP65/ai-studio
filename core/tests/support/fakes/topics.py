"""Hand-written doubles for the two raw-connection ports the broadcaster holds.

`FakeNotifier` records what was sent instead of reaching Postgres. `FakeListener` hands back
whatever a test queued, so the fan-out can be driven without a database.
"""

from __future__ import annotations

import asyncio


class FakeNotifier:
    """Records every `(channel, payload)` instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.closed = False
        self.fail_on: set[str] = set()

    async def notify(self, channel: str, payload: str) -> None:
        """Record the send, or raise when `payload` is in `fail_on`."""
        if payload in self.fail_on:
            msg = f"cannot send {payload}"
            raise RuntimeError(msg)
        self.sent.append((channel, payload))

    async def close(self) -> None:
        """Mark it closed."""
        self.closed = True


class FakeListener:
    """Hands back payloads a test queued with `arrive`, or raises what was queued with `fail_next`."""

    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.open_count = 0
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._failures: list[Exception] = []
        self._open_failures: list[Exception] = []

    def arrive(self, payload: str) -> None:
        """Queue one payload for the next `get`."""
        self._queue.put_nowait(payload)

    def fail_next(self, error: Exception) -> None:
        """Make the next `get` raise `error` instead of waiting on the queue."""
        self._failures.append(error)

    def fail_next_open(self, error: Exception) -> None:
        """Make the next `open` raise `error` instead of succeeding."""
        self._open_failures.append(error)

    async def open(self) -> None:
        """Count the call, then mark it open, or raise the next scripted open failure."""
        self.open_count += 1
        if self._open_failures:
            raise self._open_failures.pop(0)
        self.opened = True

    async def get(self) -> str:
        """The next queued payload, waiting for one, or the next scripted failure."""
        if self._failures:
            raise self._failures.pop(0)
        return await self._queue.get()

    async def close(self) -> None:
        """Mark it closed."""
        self.closed = True
