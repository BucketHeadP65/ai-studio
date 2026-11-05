"""Hand-written doubles for the two raw-connection ports the broadcaster holds and the connection a `Notifier` opens.

`FakeNotifier` records what was sent instead of reaching Postgres. `FakeListener` hands back
whatever a test queued, so the fan-out can be driven without a database. `FakeConnector` hands a
`Notifier` scripted `FakeConnection`s, so a lost connection can be driven without a database.
"""

from __future__ import annotations

import asyncio

from asyncpg.exceptions import ConnectionDoesNotExistError, InterfaceError


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
        self.close_error: Exception | None = None
        """Raised by `close` when set, after nothing is closed: a connection that fails to close."""
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
        """Mark it closed, or raise `close_error` when it is set."""
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class FakeConnection:
    """A sending connection that records each payload, or fails as asyncpg does once closed or lost mid-statement."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.closed = False
        self.terminated = False
        self.lose_on_send = False

    async def execute(self, query: str, *args: str) -> str:
        """Record `(channel, payload)`, or raise asyncpg's error for a closed or lost connection."""
        if self.lose_on_send:
            self.closed = True
            msg = "connection was closed in the middle of operation"
            raise ConnectionDoesNotExistError(msg)
        if self.closed:
            msg = "connection is closed"
            raise InterfaceError(msg)
        channel, payload = args
        self.sent.append((channel, payload))
        return query.split(" ", 1)[0]

    def is_closed(self) -> bool:
        """Whether the database or a test closed it."""
        return self.closed

    def terminate(self) -> None:
        """Mark it terminated and closed."""
        self.terminated = True
        self.closed = True

    async def close(self) -> None:
        """Mark it closed."""
        self.closed = True


class FakeConnector:
    """Hands out scripted connections in order, or raises a scripted error in a connection's place."""

    def __init__(self, *outcomes: FakeConnection | Exception) -> None:
        self.outcomes: list[FakeConnection | Exception] = list(outcomes)
        self.dsns: list[str] = []

    async def connect(self, dsn: str) -> FakeConnection:
        """Record `dsn` and return the next scripted connection, or raise the next scripted error."""
        self.dsns.append(dsn)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
